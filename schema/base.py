import numpy as np
import itertools
import enum
from collections import namedtuple, defaultdict
from .error import * 
from .fgraph import FuncNode as F, NodeFunc
from . import fgraph
from . import util


LAYOUT = ':layout'
DEFAULT_FORMAT = ':default'

class GenMode(enum.Enum):
    Inventory = 0
    Test = 1
    Inference = 2

class ShapeKind(enum.Enum):
    """
    For describing the kind of input that defines a shape
    """
    DataTensor = 0
    List = 1
    Int = 2
    Tensor = 3
    Tensor2D = 4

class DataFormats(object):
    """
    A 'data_format' is a string like 'NCW', 'NHWC', etc.  A 'layout' is a
    notion of rank-agnostic data_format.  For 1D, 2D, 3D convolutions, the
    data_formats 'NCW', 'NCHW', 'NCDHW' all correspond to a notion of 'channel
    first' (layout 0), while the data_formats 'NWC', 'NHWC', and 'NDHWC' are
    'channel last', and given layout 1.

    {formats} is a map of data_format => (layout, rank).  The layout is an
    integer index specifying the layout.  The rank specifies RANK(rank_index)
    or None if it is agnostic to the index.
    """
    def __init__(self, arg_name, formats, rank_index):
        self.arg_name = arg_name
        if formats is None:
            self.formats = { DEFAULT_FORMAT: (0, None) }
            self.rank_index = None
        else:
            self.formats = formats
            self.rank_index = rank_index

    def default(self):
        return DEFAULT_FORMAT

    def num_layouts(self):
        return len({ lr[0] for lr in self.formats.values() })

    def all_formats(self):
        return list(self.formats.keys())

    def data_format(self, layout, ranks):
        """
        Return the data_format corresponding to the layout and rank
        combination.
        """
        it = self.formats.items()
        rank = None if self.rank_index is None else ranks[self.rank_index]
        if rank is None:
            return next((df for df, (l, _) in it if l == layout), None)
        else:
            return next((df for df, (l, r) in it if l == layout and (r is None
                or r == rank)), None)

    def layout(self, data_format):
        """
        Return the layout corresponding with this data format
        """
        if data_format not in self.formats:
            raise RuntimeError(
                f'{type(self).__qualname__}: received unknown data_format '
                f'\'{data_format}\'')
        return self.formats[data_format][0]

class RankCandidates(object):
    """
    Produce all possible rank candidates, resolving min, max, and equiv
    constraints.
    """
    def __init__(self, op):
        self.op = op

        # sig => max_rank
        self.maxs = {}

        # sig => min_rank
        self.mins = {}

        # index => index 
        self.equiv = {}
        # self.equiv = { k: k for k in self.op.index.keys() }

    def equate_ranks(self, target_index, source_index):
        self.equiv[target_index] = source_index

    def add_rank_limits(self, sig, min_val, max_val):
        if min_val is not None:
            prev_min_val = self.mins.get(sig, -1)
            self.mins[sig] = max(prev_min_val, min_val)
        if max_val is not None:
            prev_max_val = self.maxs.get(sig, 10000)
            self.maxs[sig] = min(prev_max_val, max_val)

    def index_limited(self, index):
        return index in self.mins or index in self.maxs

    def index_equated(self, index):
        return index in self.equiv
    
    def all_index_ranks(self):
        fi = [ k for k in self.op.index.keys() if k not in self.equiv ]
        min_map = { tuple(fi.index(s) for s in sig): rank for sig, rank in
                self.mins.items() } 
        max_map = { tuple(fi.index(s) for s in sig): rank for sig, rank in
                self.maxs.items() } 
        gen = util.feasible_region(len(fi), min_map, max_map)
        def add_equiv(gen):
            for ranks in gen:
                rank_map = dict(zip(fi, ranks))
                eq_map = { t: rank_map[s] for t,s in self.equiv.items() }
                rank_map.update(**eq_map)
                yield rank_map
        return add_equiv(gen)

class RankConstraint(object):
    """
    Define a constraint rank(sig) == rank_func(shape), where sig and shape are
    the run-time signature and shape associated with {shape_arg}
    """
    def __init__(self, name, shape_arg, rank_func):
        self.name = name
        self.shape_arg = shape_arg
        self.rank_func = rank_func

    def observed_rank(self, shape_map, **kwargs):
        # return the observed rank of the associated shape argument
        # this takes **kwargs because sometimes, rank information comes from
        # other sources besides the shape_map
        shape = shape_map[self.shape_arg]
        return self.rank_func(shape)

    def computed_rank(self, sig_map, rank_map):
        # return the rank of the associated signature that is implied by the
        # index ranks
        sig = sig_map[self.shape_arg]
        return sum(rank_map[s] for s in sig)

    def rank_error(self, sig_map, shape_map, rank_map, **kwargs):
        """
        Computes the difference between the predicted rank of the constraint's
        argument's signature based on the proposed set of index ranks, and the
        observed rank.
        Negative means the fix is to add to the rank

        The observed rank can be None, which means the observation doesn't
        determine the rank.  Since it is unknown, this doesn't represent any
        evidence of error.  (See function shape_rank)
        """
        obs_rank = self.observed_rank(shape_map, **kwargs) 
        cmp_rank = self.computed_rank(sig_map, rank_map)
        if obs_rank is None:
            return 0
        else:
            return obs_rank - cmp_rank

    def highlight_map(self):
        """
        Produce a map of arg_name => [dim1, dim2, ...], where dim1 etc are
        positions of the shape that should be highlighted with '^^'.
        """
        raise NotImplementedError

    def suggestion(self):
        """
        A plain-English suggestion to the user, describing what aspect of the
        input needs to be changed.
        """
        raise NotImplementedError

def shape_rank(shape):
    # returns the rank of shape
    if isinstance(shape, list):
        return len(shape)
    elif isinstance(shape, int):
        return None
    else:
        raise SchemaError(
            f'shape_rank: invalid shape type.  expected list or int, got '
            f'{type(shape)}: {shape}')

def shape_iter(shape):
    # returns an iterator for shape's components, interpreting an integer as
    # broadcastable
    def loop():
        while True:
            yield shape
    if isinstance(shape, list):
        return iter(shape)
    elif isinstance(shape, int):
        return loop()

def shape_nextn(shape_iter, n):
    # return the next n elements from shape_iter
    return [ next(shape_iter) for _ in range(n) ]

class SliceRankConstraint(RankConstraint):
    def __init__(self, shape_arg, slice_index):
        """
        Represent the logical constraint:

        rank(sig) == len(shape)

        where sig and shape are the signature and shape associated with
        {shape_arg}.{slice_index}.  These special nodes are created by the API
        call arg_shape_tensor2d.
        """
        node = f'{shape_arg}.{slice_index}'
        name = f'rank(sig({node})) == len({node})'
        
        super().__init__(name, node, shape_rank)
        self.arg_name = shape_arg

    def highlight_map(self, sig_map, shape_map, rank_map):
        obs_rank = self.observed_rank(shape_map)
        cmp_rank = self.computed_rank(sig_map, rank_map)
        lo = min(obs_rank, cmp_rank)
        hi = max(obs_rank, cmp_rank)
        inds = list(range(lo, hi))
        return { self.shape_arg: inds }

    def suggestion(self, rank_error):
        if rank_error == 0:
            return None
        elif rank_error < 0:
            msg = f'Increase {self.arg_name}.shape[1] by {-rank_error}'
        else:
            msg = f'Decrease {self.arg_name}.shape[1] by {rank_error}'

class ShapeRankConstraint(RankConstraint):
    """
    Represent the logical constraint:

    rank(sig) == len(shape)

    where sig and shape are the signature and shape associated with
    {shape_arg}.

    {shape_arg} Kind may be one of DATA_TENSOR, SHAPE_INT, SHAPE_LIST,
    SHAPE_TENSOR, SHAPE_TENSOR2D 
    """
    def __init__(self, shape_arg, arg_type):
        name = f'rank(sig({shape_arg})) == len({shape_arg})'
        super().__init__(name, shape_arg, shape_rank)
        self.arg_type = arg_type
        
    def highlight_map(self, sig_map, shape_map, rank_map):
        re = self.rank_error(sig_map, shape_map, rank_map)
        shape = shape_map[self.shape_arg]
        act_len = len(shape)
        cmp_len = act_len - re
        inds = list(range(min(act_len, cmp_len), max(act_len, cmp_len)))
        return { self.shape_arg: inds }

    def suggestion(self, rank_error):
        s = 's' if abs(rank_error) > 1 else ''
        if rank_error == 0:
            return None
        elif rank_error < 0:
            if self.arg_type == ShapeKind.DataTensor:
                msg = f'Add {-rank_error} dimension{s} to \'{self.shape_arg}\''
            elif self.arg_type in (ShapeKind.Tensor, ShapeKind.List):
                msg = f'Add {-rank_error} element{s} to \'{self.shape_arg}\''
            elif self.arg_type == ShapeKind.Int:
                msg = f'Increase \'{self.shape_arg}\' by {-rank_error}'
            else:
                pass
        else:
            if self.arg_type == ShapeKind.Tensor:
                msg = (f'Remove {rank_error} dimension{s} from '
                f'\'{self.shape_arg}\'')
            elif self.arg_type in (ShapeKind.Tensor, ShapeKind.List):
                msg = (f'Remove {rank_error} element{s} from '
                        f'\'{self.shape_arg}\'')
            elif self.arg_type == ShapeKind.Int:
                msg = f'Decrease \'{self.shape-arg}\' by {-rank_error}'
        return msg

class IntRankConstraint(RankConstraint):
    """
    Define the constraint: rank(rank_sig) == arg_val, where arg_val is the
    value of {shape_arg}
    """
    def __init__(self, name, rank_arg, rank_sig):
        super().__init__(name, None, None)
        self.rank_sig = rank_sig
        self.rank_arg = rank_arg

    def observed_rank(self, _, **kwargs):
        val = kwargs[self.rank_arg]
        return val

    def computed_rank(self, sig_map, rank_map):
        sig = self.rank_sig
        return sum(rank_map[s] for s in sig)

    def highlight_map(self, *args):
        return { self.rank_arg: [0] }

    def suggestion(self, rank_error):
        if rank_error == 0:
            return None
        elif rank_error < 0:
            return f'Increase \'{self.shape_arg}\' by {-rank_error}'
        else:
            return f'Decrease \'{self.shape_arg}\' by {rank_error}'

class DimRankConstraint(RankConstraint):
    """
    Define a constraint called {name} with the logic:

    dims(source_idx)[0] = get_dims_func(shape)

    """
    def __init__(self, name, rank_sig, shape_arg, get_dims_func, source_idx):
        super().__init__(name, shape_arg, get_dims_func)
        self.rank_sig = rank_sig 
        self.source_idx = source_idx

    def computed_rank(self, _, rank_map):
        sig = self.rank_sig
        return sum(rank_map[s] for s in sig)

    def highlight_map(self, sig_map, shape_map, rank_map):
        hl = defaultdict(list) 
        for arg, shape in shape_map.items():
            sig = sig_map[arg]
            dim = 0
            for s in sig:
                if s == self.source_idx:
                    hl[arg].extend(range(dim, dim + rank_map[s]))
                dim += rank_map[s]
        return hl

    def suggestion(self, rank_error):
        if rank_error == 0:
            return None
        elif rank_error < 0:
            return (f'Increase the dimension of index \'{self.source_idx}\' by '
                    f'{-rank_error}')
        else:
            return (f'Decrease the dimension of index \'{self.source_idx}\' by '
                    f'{rank_error}')

class CompIndex(NodeFunc):
    # FuncNode object for indices registered with computed_index
    # {comp_func} 
    def __init__(self, idx, comp_func, extra_arg_names):
        super().__init__(idx)
        self.func = comp_func
        self.extra_names = extra_arg_names

    def __call__(self, *args):
        # args[:-1] will be index dims
        # args[-1] will be a kwargs map
        index_args = [ np.array(a) for a in args[:-1] ]
        kwargs = args[-1]
        extra = tuple(kwargs[k] for k in self.extra_names)
        comp_dims = self.func(*index_args, *extra)
        if not (isinstance(comp_dims, np.ndarray) and comp_dims.ndim == 1):
            raise SchemaError(
                f'{type(self).__qualname__}: function \'{self.func.__name__}\' '
                f'registered with computed_dims must return a 1D '
                f'np.ndarray.  Got \'{comp_dims}\'')
        comp_dims = comp_dims.tolist()
        return comp_dims

class GenIndex(object):
    """
    Generate dimensions for {output_indices} using {gen_func}.  Used in
    Kind.GEN_DIMS nodes.  Has parent Kind.RANKS

    Calls gen_func(ranks_list, *gen_args).  ranks_list are the ranks of each
    index in {input_indices} in order.

    returns a list of shape tuples, one shape for each index in output_indices.
    A shape is an integer list.  

    For example, if output_indices has two indices, a return value could be:
    [ 
      ([1,2,3], [4,5]),
      ([6,4,2], [5,4]) 
    ]
    """
    def __init__(self, gen_func, output_indices, input_indices, gen_args):
        self.output_indices = output_indices 
        self.input_indices = input_indices 
        self.func = gen_func
        self.gen_args = gen_args

    @staticmethod
    def valid_return(vals):
        return (
                isinstance(vals, list) and
                all(isinstance(v, tuple) for v in vals) and
                all(isinstance(s, list) for v in vals for s in v)
                )

    def __call__(self, ranks_map):
        ranks_list = [ ranks_map[i] for i in self.input_indices ]
        vals = self.func(ranks_list, *self.gen_args)
        if not self.valid_return(vals):
            raise SchemaError(
                f'{type(self).__qualname__}: Custom Dims generation function '
                f'\'{self.func.__name__}\' returned the wrong type.  Expected '
                f'a list of shape tuples, for example like: \n'
                f'[ \n'
                f'  ([1,2,3], [4,5]),\n'
                f'  ([6,4,2], [5,4]) \n'
                f'].\n'
                f'Got: {vals}\n')
        return [ dict(zip(self.output_indices,v)) for v in vals ]

class GenIndices(object):
    """
    Aggregator for GenIndex
    """
    def __init__(self):
        self.generators = []
    
    def add_generator(self, gen_func, output_idxs, input_idxs, gen_args=()):
        gen = GenIndex(gen_func, output_idxs, input_idxs, gen_args)
        self.generators.append(gen)

    def __call__(self, index_ranks):
        index_dims_list = []
        lists = [ gen(index_ranks) for gen in self.generators ]
        for tup in itertools.product(*lists):
            dims_map = { k:v  for t in tup for k,v in t.items() }
            index_dims_list.append(dims_map)
        return index_dims_list

class InputVar(NodeFunc):
    def __init__(self, name):
        super().__init__(name)

    def __call__(self):
        return None

class CompDimsGraph(object):
    """
    Represents the computation graph to calculate computed dims which appear
    in a data tensor signature.  It may compute intermediate computed dims as
    well.
    """
        
    def __init__(self):
        self.input_indices = {} # idx => FuncNode(InputVar)
        self.comp_indices = {}  # idx => FuncNode(CompIndex)
        self.nodes = {}
        F.set_registry(self.nodes)
        node = F.add_node_sn(InputVar('kwargs'))
        self.kwnode = node

    def maybe_add_input_index(self, idx):
        node = self.input_indices.get(idx, None)
        if node is None:
            node = F.add_node_sn(InputVar(idx))
            self.input_indices[idx] = node
        return node

    def add_comp_index(self, idx, comp_func, parent_indexes, *const_args):
        """
        Adds computed index {idx}, whose value will be computed with a call to:
        {comp_func}(*index_dims, *const_vals)

        index_dims are the dimensions from {parent_indices} (a signature-like
        string).

        {const_args} are names which must appear as keys in __call__ **kwargs
        """
        parents = []
        for pidx in parent_indexes:
            node = self.comp_indices.get(pidx, None)
            if node is None:
                node = self.maybe_add_input_index(pidx)
            parents.append(node)
        
        ci_obj = CompIndex(idx, comp_func, const_args)
        node = F.add_node_sn(ci_obj, *parents)
        self.comp_indices[idx] = node

    def computed_indexes(self):
        # return a string of computed indices
        return ''.join(self.comp_indices.keys())

    def input_indexes(self):
        return ''.join(self.input_indices.keys())

    def get_index_inputs(self, computed_index):
        """
        Retrieve index inputs for {computed_index} 
        """
        node = self.comp_indices[computed_index]
        ancestors = fgraph.get_ancestors(node)
        index_inputs = ''.join(a.sub_name for a in ancestors if a in
                self.input_indices.values())
        return index_inputs

    def finalize(self):
        for node in self.comp_indices.values():
            node.append_parent_sn(self.kwnode)

    def __call__(self, index_dims, **kwargs):
        self.kwnode.set_cached(kwargs)
        for idx, node in self.input_indices.items():
            # that any unavailable indices are not needed for this layout
            val = index_dims.get(idx, None)
            node.set_cached(val)
        comp_nodes = list(self.comp_indices.values())

        # this is node name => value
        val_map = fgraph.func_graph_evaluate(*comp_nodes)
        return val_map

class Constraint(object):
    """
    Static list of argument names to retrieve from a map
    """
    def __init__(self, *names):
        self.arg_names = names

    def get_argnames(self):
        return self.arg_names

class SumRangeConstraint(Constraint):
    """
    Expresses the constraint RANK(sig) in [lo, hi].  When called, it provides a
    residual range based on values provided for some subset of indexes in the
    signature.
    """
    def __init__(self, op, sig, lo, hi):
        super().__init__()
        self.op = op
        self.sig = sig
        self.lo = lo
        self.hi = hi

    def __repr__(self):
        return f'{type(self).__name__}: RANK({self.sig}) in [{self.lo}, {self.hi}]'

    def __call__(self, **index_ranks):
        residual = sum(index_ranks.get(idx, 0) for idx in self.sig)
        return (
                max(0, self.lo - residual - self.op.avail_edits), 
                max(0, self.hi - residual + self.op.avail_edits)
                )

class ArgRankConstraint(Constraint):
    """
    Used during the GenMode.Inference phase
    Expresses one of these constraints:
    1. RANK(SIG(arg)) = RANK(arg)   (if with_low_bound is True)
    2. RANK(SIG(arg)) in [0, RANK(arg)]   (otherwise)

    """
    def __init__(self, op, arg_name, with_low_bound=False):
        super().__init__('shapes', 'sigs')
        self.arg_name = arg_name
        self.op = op
        self.with_low_bound = with_low_bound

    def __repr__(self):
        r = f'{type(self).__name__}: RANK(SIG({self.arg_name}))'
        if self.with_low_bound:
            r += f' = RANK({self.arg_name})'
        else:
            r += f' in [0, RANK({self.arg_name})]'
        return r

    def __call__(self, obs_shapes, sigs, **index_ranks):
        if self.op.generation_mode != GenMode.Inference:
            return 0, 10000
        # get arg signature and shape
        arg_sig = sigs[self.arg_name]
        obs_shape = obs_shapes[self.arg_name]
        if isinstance(obs_shape, int):
            # rank is indeterminate
            return 0, 10000 
        obs_rank = len(obs_shape)
        tlo = obs_rank if self.with_low_bound else 0
        thi = obs_rank 
        residual = sum(index_ranks.get(idx, 0) for idx in arg_sig)
        return max(0, tlo - residual), max(0, thi - residual)

