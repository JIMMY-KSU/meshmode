from __future__ import division, print_function

__copyright__ = """
Copyright (C) 2018 Andreas Kloeckner
Copyright (C) 2014-6 Shivam Gupta
Copyright (C) 2016 Matt Wala
"""

__license__ = """
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.
"""


import numpy as np
from six.moves import range  # noqa: F401
from pytools import RecordWithoutPickling

from pytools import memoize_method


import logging
logger = logging.getLogger(__name__)


class _TesselationInfo(RecordWithoutPickling):

    def __init__(self, children, ref_vertices, orig_vertex_indices,
            midpoint_indices, midpoint_vertex_pairs, resampler):
        RecordWithoutPickling.__init__(self,
                children=children,
                ref_vertices=ref_vertices,
                orig_vertex_indices=orig_vertex_indices,
                midpoint_indices=midpoint_indices,
                midpoint_vertex_pairs=midpoint_vertex_pairs,
                resampler=resampler)


class _GroupRefinementRecord(RecordWithoutPickling):

    def __init__(self, tesselation, element_mapping):
        RecordWithoutPickling.__init__(self,
            tesselation=tesselation, element_mapping=element_mapping)


class RefinerWithoutAdjacency(object):
    """A refiner that may be applied to non-conforming
    :class:`meshmode.mesh.Mesh` instances. It does not generate adjacency
    information, and it is typically faster than
    :class:`meshmode.mesh.refinement.Refiner`.

    .. note::

        If the input meshes to this refiner are not conforming, then
        the resulting meshes may contain duplicated vertices.
        (I.e. two different numbers referring to the same geometric
        vertex.)
    """

    def __init__(self, mesh):
        self._current_mesh = mesh
        self._previous_mesh = None
        self.group_refinement_records = None
        self.global_vertex_pair_to_midpoint = {}

    # {{{ build tesselation info

    @memoize_method
    def _get_bisection_tesselation_info(self, group_type, dim):
        from meshmode.mesh import SimplexElementGroup
        if issubclass(group_type, SimplexElementGroup):
            from meshmode.mesh.refinement.tesselate import \
                    tesselate_simplex_bisection, add_tuples, halve_tuple
            ref_vertices, children = tesselate_simplex_bisection(dim)

            orig_vertex_tuples = [(0,) * dim] + [
                    (0,) * i + (2,) + (0,) * (dim-i-1)
                    for i in range(dim)]
            node_dict = dict(
                  (ituple, idx)
                  for idx, ituple in enumerate(ref_vertices))
            orig_vertex_indices = [node_dict[vt] for vt in orig_vertex_tuples]

            from meshmode.mesh.refinement.resampler import SimplexResampler
            resampler = SimplexResampler()
            vertex_pair_to_midpoint_order = \
                    resampler.get_vertex_pair_to_midpoint_order(dim)

            midpoint_idx_to_vertex_pair = {}
            for vpair, mpoint_idx in vertex_pair_to_midpoint_order.items():
                midpoint_idx_to_vertex_pair[mpoint_idx] = vpair

            midpoint_vertex_pairs = [
                    midpoint_idx_to_vertex_pair[i]
                    for i in range(len(midpoint_idx_to_vertex_pair))]

            midpoint_indices = [
                    node_dict[
                        halve_tuple(
                            add_tuples(
                                orig_vertex_tuples[v1],
                                orig_vertex_tuples[v2]))]
                    for v1, v2 in midpoint_vertex_pairs]

            return _TesselationInfo(
                    ref_vertices=ref_vertices,
                    children=np.array(children),
                    orig_vertex_indices=np.array(orig_vertex_indices),
                    midpoint_indices=np.array(midpoint_indices),
                    midpoint_vertex_pairs=midpoint_vertex_pairs,
                    resampler=resampler,
                    )

        else:
            raise NotImplementedError(
                    "bisection for elements groups of type %s"
                    % group_type.__name__)

    # }}}

    def refine_uniformly(self):
        flags = np.ones(self._current_mesh.nelements, dtype=bool)
        self.refine(flags)

    # {{{ refinement top-level

    def refine(self, refine_flags):
        """
        :arg refine_flags: a :class:`numpy.ndarray` of dtype bool of length
            ``mesh.nelements`` indicating which elements should be split.
        """

        mesh = self._current_mesh

        refine_flags = np.asarray(refine_flags, dtype=np.bool)

        if len(refine_flags) != mesh.nelements:
            raise ValueError("length of refine_flags does not match "
                    "element count of last generated mesh")

        new_el_groups = []
        group_refinement_records = []
        additional_vertices = []
        inew_vertex = mesh.nvertices

        for igrp, group in enumerate(mesh.groups):
            bisection_info = self._get_bisection_tesselation_info(
                    type(group), group.dim)

            # {{{ compute counts and index arrays

            grp_flags = refine_flags[
                    group.element_nr_base:
                    group.element_nr_base+group.nelements]

            nchildren = len(bisection_info.children)
            nchild_elements = np.ones(group.nelements, dtype=mesh.element_id_dtype)
            nchild_elements[grp_flags] = nchildren

            child_el_indices = np.empty(
                    group.nelements+1, dtype=mesh.element_id_dtype)
            child_el_indices[0] = 0
            child_el_indices[1:] = np.cumsum(nchild_elements)

            unrefined_el_new_indices = child_el_indices[:-1][~grp_flags]
            refining_el_old_indices, = np.where(grp_flags)

            new_nelements = child_el_indices[-1]

            # }}}

            group_refinement_records.append(
                    _GroupRefinementRecord(
                        tesselation=bisection_info,
                        element_mapping=[
                            list(range(
                                child_el_indices[iel],
                                child_el_indices[iel]+nchild_elements[iel]))
                            for iel in range(group.nelements)]))

            # {{{ get new vertices together

            midpoints = bisection_info.resampler.get_midpoints(
                    group, bisection_info, refining_el_old_indices)

            new_vertex_indices = np.empty(
                (new_nelements, group.vertex_indices.shape[1]),
                dtype=mesh.vertex_id_dtype)
            new_vertex_indices.fill(-17)

            # copy over unchanged vertices
            new_vertex_indices[unrefined_el_new_indices] = \
                    group.vertex_indices[~grp_flags]

            for old_iel in refining_el_old_indices:
                new_iel_base = child_el_indices[old_iel]

                refining_vertices = np.empty(len(bisection_info.ref_vertices),
                        dtype=mesh.vertex_id_dtype)
                refining_vertices.fill(-17)

                # carry over old vertices
                refining_vertices[bisection_info.orig_vertex_indices] = \
                        group.vertex_indices[old_iel]

                for imidpoint, (iref_midpoint, (v1, v2)) in enumerate(zip(
                        bisection_info.midpoint_indices,
                        bisection_info.midpoint_vertex_pairs)):

                    global_v1 = group.vertex_indices[old_iel, v1]
                    global_v2 = group.vertex_indices[old_iel, v2]

                    if global_v1 > global_v2:
                        global_v1, global_v2 = global_v2, global_v1

                    try:
                        global_midpoint = self.global_vertex_pair_to_midpoint[
                                global_v1, global_v2]
                    except KeyError:
                        global_midpoint = inew_vertex
                        additional_vertices.append(midpoints[old_iel][:, imidpoint])
                        inew_vertex += 1

                    refining_vertices[iref_midpoint] = global_midpoint

                assert (refining_vertices >= 0).all()

                new_vertex_indices[new_iel_base:new_iel_base+nchildren] = \
                        refining_vertices[bisection_info.children]

            assert (new_vertex_indices >= 0).all()

            # }}}

            # {{{ get new nodes together

            new_nodes = np.empty(
                (mesh.ambient_dim, new_nelements, group.nunit_nodes),
                dtype=group.nodes.dtype)

            new_nodes.fill(float("nan"))

            # copy over unchanged nodes
            new_nodes[:, unrefined_el_new_indices] = group.nodes[:, ~grp_flags]

            tesselated_nodes = bisection_info.resampler.get_tesselated_nodes(
                    group, bisection_info, refining_el_old_indices)

            for old_iel in refining_el_old_indices:
                new_iel_base = child_el_indices[old_iel]
                new_nodes[:, new_iel_base:new_iel_base+nchildren, :] = \
                        tesselated_nodes[old_iel]

            assert (~np.isnan(new_nodes)).all()

            # }}}

            new_el_groups.append(
                type(group)(
                    order=group.order,
                    vertex_indices=new_vertex_indices,
                    nodes=new_nodes,
                    unit_nodes=group.unit_nodes))

        new_vertices = np.empty(
                (mesh.ambient_dim, mesh.nvertices + len(additional_vertices)),
                mesh.vertices.dtype)
        new_vertices[:, :mesh.nvertices] = mesh.vertices
        new_vertices[:, mesh.nvertices:] = np.array(additional_vertices).T

        from meshmode.mesh import Mesh
        new_mesh = Mesh(new_vertices, new_el_groups, is_conforming=(
            mesh.is_conforming
            and (refine_flags.all() or (~refine_flags).all())))

        self.group_refinement_records = group_refinement_records
        self._current_mesh = new_mesh
        self._previous_mesh = mesh

        return new_mesh

    # }}}

    def get_current_mesh(self):
        return self._current_mesh

    def get_previous_mesh(self):
        return self._previous_mesh


# vim: foldmethod=marker
