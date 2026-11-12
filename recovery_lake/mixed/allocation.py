"""Deterministic proportional allocation of one batch over material x stream cells.

For one batch let ``a_i`` be the mass of material i (g), ``b_j`` the mass of
stream j (g), and ``N = sum(a_i) = sum(b_j)``.  The contract asks for integers

    x_ij = base_ij + u_ij,   base_ij = (a_i*b_j)//N,  u_ij in {0,1}
    sum_j x_ij = a_i,        sum_i x_ij = b_j

with ``u_ij = 0`` whenever ``r_ij = (a_i*b_j)%N`` is zero, maximising
``sum(r_ij*u_ij)`` and, among optima, choosing the lexicographically largest
u vector in (material_id, stream_id) Unicode code point order.

Why this is a flow problem
--------------------------
``base`` already uses at most each row and column margin, so the row and
column deficits are ``R_i = a_i - sum_j base_ij`` and
``C_j = b_j - sum_i base_ij``, both integer and with equal totals.  Keeping
only cells with ``r_ij > 0`` leaves a bipartite b-matching instance: each cell
edge has capacity 1, source -> material has capacity ``R_i``, stream -> sink
has capacity ``C_j``.  A unit of flow on cell (i,j) is exactly ``u_ij = 1``.

Feasibility of this restricted support follows from total unimodularity: the
fractional remainders ``r_ij/N`` form a feasible point of the capped
transportation polytope on that same support, whose vertices are integral.
Thus a 0/1 completion with the required margins always exists, and no
zero-remainder cell ever needs to be rounded up.

The primary objective ``sum r_ij*u_ij`` is encoded in one integer edge weight
per cell; lexicographic tie-breaking among equal primary objectives is encoded
by adding binary place-value bonuses in sorted cell order.  With ``k`` cells,
``base_weight = 2**k`` and ``bonus_ij = 2**(k-1-rank)``, the total bonus of any
solution is at most ``2**k - 1``, so a one-point loss of primary objective
always outweighs every possible bonus.  A single min-cost max-flow therefore
returns the unique contract optimum directly.

The implementation is min-cost max-flow (successive shortest paths with
potentials and a blocking flow per potential phase).  It is polynomial in the
number of material x stream cells, uses exact Python integers for every
product, modulo and weight (no 64-bit overflow, no float), and never loops over
grams or enumerates 0/1 matrices.
"""
import heapq
from collections import namedtuple

Cell = namedtuple("Cell", "material_id stream_id base remainder denominator rounded_up allocated")


class _MinCostFlow:
    """Sparse min-cost max-flow with integer costs and Dijkstra potentials."""

    def __init__(self, nodes):
        self.nodes = list(range(nodes))
        self.to = []
        self.capacity = []
        self.cost = []
        self.reverse = []
        self.out = [[] for _ in self.nodes]

    def add(self, u, v, capacity, cost):
        forward = len(self.to)
        self.to.append(v); self.capacity.append(capacity); self.cost.append(cost); self.reverse.append(forward + 1)
        self.to.append(u); self.capacity.append(0); self.cost.append(-cost); self.reverse.append(forward)
        self.out[u].append(forward)
        self.out[v].append(forward + 1)
        return forward

    def run(self, source, sink):
        """Push the maximum source->sink flow; return the total and used edges.

        Primal-dual: each phase computes shortest reduced distances from
        ``source`` (Dijkstra with potentials) and then sends a blocking flow
        along all zero-reduced-cost residual edges (Dinic-style DFS).  This
        preserves min-cost correctness while replacing up to one Dijkstra per
        gram with at most one Dijkstra per potential level, keeping the solver
        polynomial in the cell matrix.
        """
        # Initial admissible potentials.  Original edges form a DAG
        # source -> materials -> streams -> sink, so relax in topological
        # order; every edge starts with all capacity forward.
        potential = [0] * len(self.nodes)
        for _ in range(len(self.nodes)):
            changed = False
            for node in self.nodes:
                for edge in self.out[node]:
                    if self.capacity[edge] > 0:
                        candidate = potential[node] + self.cost[edge]
                        if candidate < potential[self.to[edge]]:
                            potential[self.to[edge]] = candidate
                            changed = True
            if not changed:
                break

        flow = 0
        while True:
            distances = [None] * len(self.nodes)
            distances[source] = 0
            heap = [(0, source)]
            while heap:
                distance, node = heapq.heappop(heap)
                if distance > distances[node]:
                    continue
                for edge in self.out[node]:
                    if self.capacity[edge] <= 0:
                        continue
                    other = self.to[edge]
                    reduced = self.cost[edge] + potential[node] - potential[other]
                    candidate = distance + reduced
                    if distances[other] is None or candidate < distances[other]:
                        distances[other] = candidate
                        heapq.heappush(heap, (candidate, other))
            if distances[sink] is None:
                break
            for node in self.nodes:
                if distances[node] is not None:
                    potential[node] += distances[node]

            # Blocking flow over zero-reduced-cost residual edges.
            while True:
                pointer = [0] * len(self.nodes)
                pushed = self._augment(source, sink, pointer, potential)
                if pushed == 0:
                    break
                flow += pushed
        return flow

    def _augment(self, source, sink, pointer, potential):
        """Iterative Dinic DFS over admissible (zero reduced cost) edges."""
        stack = [source]
        path = []  # residual edges along the current simple path
        on_path = {source}
        while stack:
            node = stack[-1]
            if node == sink:
                push = min(self.capacity[edge] for edge in path)
                for edge in path:
                    self.capacity[edge] -= push
                    self.capacity[self.reverse[edge]] += push
                return push
            edges = self.out[node]
            advanced = False
            while pointer[node] < len(edges):
                edge = edges[pointer[node]]
                other = self.to[edge]
                if (self.capacity[edge] > 0 and other not in on_path
                        and self.cost[edge] + potential[node] - potential[other] == 0):
                    stack.append(other)
                    on_path.add(other)
                    path.append(edge)
                    advanced = True
                    break
                pointer[node] += 1
            if not advanced:
                pointer[node] = len(edges)
                stack.pop()
                on_path.discard(node)
                if path:
                    path.pop()
                    # The edge we came through is exhausted for this phase.
                    if stack:
                        pointer[stack[-1]] += 1
        return 0


def allocate_batch(materials, streams):
    """Allocate one batch and return (cells, denominator N).

    ``materials`` is an iterable of (material_id, total_grams), already
    aggregated per material.  ``streams`` is an iterable of
    (stream_id, total_grams), one per weighed stream.  Both totals must be
    positive and equal.
    """
    materials = list(materials)
    streams = list(streams)
    if not materials or not streams:
        raise ValueError("a batch needs at least one material and one stream")
    for _, mass in materials:
        if not isinstance(mass, int) or mass <= 0:
            raise ValueError("material mass must be a positive integer gram value")
    for _, mass in streams:
        if not isinstance(mass, int) or mass <= 0:
            raise ValueError("stream mass must be a positive integer gram value")
    total = sum(mass for _, mass in materials)
    if total != sum(mass for _, mass in streams):
        raise ValueError("batch material total must equal stream total")

    ids = [material_id for material_id, _ in materials]
    if len(set(ids)) != len(ids):
        raise ValueError("material identifiers must be unique within a batch")
    stream_ids = [stream_id for stream_id, _ in streams]
    if len(set(stream_ids)) != len(stream_ids):
        raise ValueError("stream identifiers must be unique within a batch")

    rows, columns = len(materials), len(streams)
    base = [[0] * columns for _ in range(rows)]
    remainder = [[0] * columns for _ in range(rows)]
    for i, (_, a) in enumerate(materials):
        for j, (_, b) in enumerate(streams):
            # Exact integer product: arbitrary precision, no float, no overflow.
            base[i][j], remainder[i][j] = divmod(a * b, total)

    row_deficit = [a - sum(base[i]) for i, (_, a) in enumerate(materials)]
    column_deficit = [
        b - sum(base[i][j] for i in range(rows)) for j, (_, b) in enumerate(streams)
    ]
    if sum(row_deficit) != sum(column_deficit):
        raise AssertionError("row and column rounding deficits must be equal")

    # All cells in contract tie-break order: Python string comparison is by
    # Unicode code point; CSV row order and global ranks are not used.
    ordered = sorted(
        ((i, j) for i in range(rows) for j in range(columns)),
        key=lambda cell: (ids[cell[0]], stream_ids[cell[1]]),
    )
    k = len(ordered)
    positional = {cell: rank for rank, cell in enumerate(ordered)}
    # Every positional bonus is < 2^k, so one remainder point outweighs all
    # bonuses together; the multiplier is exactly 2^k == 1 << k.
    weight_base = 1 << k

    source = rows + columns
    sink = source + 1
    graph = _MinCostFlow(sink + 1)
    material_node = [i for i in range(rows)]
    stream_node = [rows + j for j in range(columns)]
    for i in range(rows):
        if row_deficit[i]:
            graph.add(source, material_node[i], row_deficit[i], 0)
    for j in range(columns):
        if column_deficit[j]:
            graph.add(stream_node[j], sink, column_deficit[j], 0)
    cell_edges = {}
    for (i, j) in ordered:
        if remainder[i][j] == 0:
            continue  # rule 1: zero remainder can never be rounded up
        rank = positional[(i, j)]
        bonus = 1 << (k - 1 - rank)
        # Larger remainder first, then earlier code-point cell, as one integer.
        # The bonus fits in k bits, so a one-gram remainder loss always loses.
        score = remainder[i][j] * weight_base + bonus
        cell_edges[(i, j)] = graph.add(material_node[i], stream_node[j], 1, -score)

    flow = graph.run(source, sink)
    if flow != sum(row_deficit):
        raise AssertionError("no feasible 0/1 rounding exists for batch")

    rounded_up = [[0] * columns for _ in range(rows)]
    for (i, j), edge in cell_edges.items():
        rounded_up[i][j] = 1 - graph.capacity[edge]

    # Independent check of both margins before the result leaves this module.
    for i in range(rows):
        if sum(base[i][j] + rounded_up[i][j] for j in range(columns)) != materials[i][1]:
            raise AssertionError("material row margin violated after allocation")
    for j in range(columns):
        if sum(base[i][j] + rounded_up[i][j] for i in range(rows)) != streams[j][1]:
            raise AssertionError("stream column margin violated after allocation")

    cells = []
    for i, (material_id, _) in enumerate(materials):
        for j, (stream_id, _) in enumerate(streams):
            cells.append(Cell(
                material_id, stream_id, base[i][j], remainder[i][j], total,
                rounded_up[i][j], base[i][j] + rounded_up[i][j],
            ))
    return cells, total
