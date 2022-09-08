import traceback
import tensorflow as tf
import numpy as np
from collections import OrderedDict
from . import predicates as pr
from . import generators as ge
from . import mutations
from . import base
from .base import Kind, kname, kpfx, kind
from . import fgraph
from .error import *
from .fgraph import PredNode as P, GenNode as G

"""
Every API call will mutate the Generative Graph and the Predicate Graph
logically in tandem.  It should maintain the invariants:

1. Every value set produced by the Generative Graph should be valid as judged
   by the Predicate Graph

2. The set of values produced by the Generative Graph is "complete" in the
sense that it it explores every combination of dtypes, ranks, and option
arguments.  It need not explore every possible setting of dimensions or tensor
contents.

Both finished graphs must have exactly one node corresponding to each input
argument, either the arg_name:tensor node (for tensor arguments) or the
arg_name:arg node for non-tensors.
"""

class SchemaApi(object):
    def __init__(self, op_path):
        self.op_path = op_path

        # idx => description
        self.index = {}
        # param_name => kname (the name of the GenNode producing the param value)
        self.params = {}
        self.gen_graph = None
        self.input_pred_graph = None
        self.return_pred_graph = None
        self.rank_cons = base.RankConstraints(self)
        self.dtype_cons = base.DTypeConstraints()
        self.comp_dims = base.CompDims()
        self.num_returns = 0
        self.num_layouts = 1

        # error status
        self.input_status = None
        self.framework_status = None

        # call time values
        self.arguments = {}
        self.returns = []

    def _init_schema(self, func_sig, init_schema_func):
        pars = func_sig.parameters.keys()
        self.params = OrderedDict({ k: None for k in pars })
        self._init_pred_graph()
        self._init_gen_graph()
        init_schema_func(self)
        self._add_pred_graph()
        self._add_gen_graph()
        self._validate_constraints()

    def _prepare_call(self, bound_args):
        """Call during the framework call phase"""
        self.arguments = bound_args
        self.returns.clear()
        self.input_status = None
        self.framework_status = None

    def _check_args(self):
        """
        The main function to check all input arguments for all constraints
        registered on the schema
        """
        error = fgraph.pred_graph_evaluate(self.input_pred_graph.values())
        self.input_status = Success() if error is None else error

    def _check_return(self, op_return):
        """
        Check the return tensors' shapes and types against those predicted by
        the framework
        """
        if not isinstance(self.input_status, Success):
            return

        if not isinstance(op_return, (list, tuple)):
            op_return = (op_return,)
        self.returns = list(op_return)
        error = fgraph.pred_graph_evaluate(self.return_pred_graph.values())
        if error is not None:
            raise SchemaError(error.msg(self))

    def _log_framework_status(self, err):
        self.framework_status = FrameworkError(err)

    def _signature_instantiations(self):
        """
        Produces a list of instantiation maps like:
        arg_name => sig_instantiation
        A sig_instantiation is a signature with each letter repeated according
        to the index's rank
        """
        inst_node = self.gen_graph[Kind.SIG_INST]
        inst_list = fgraph.all_values(inst_node)
        return inst_list

    def _sig_inventory(self):
        """
        Generate the formatted signature inventory for this op
        """
        inst_list = self._signature_instantiations()
        inst1 = inst_list[0]
        arg_names = [ k for k in self.params.keys() if k in inst1 ]
        inst_groups = [arg_names]
        inst_groups.extend([l[a] for a in arg_names] for l in inst_list)
        inst_tab, _ = tabulate(inst_groups, '  ', left_align=True)
        return inst_tab

    def _index_inventory(self):
        """
        Generate a formatted report of the indices with their rank constraints
        """
        rows = []
        rows.append(['Index', 'Description'])
        rows.extend([ix,desc] for ix,desc in self.index.items())
        tab, _ = tabulate(rows, '  ', left_align=True)
        return tab

    def _validate_schema(self):
        """
        Generate a set of input argument value configurations for the op, and
        run the op on each configuration.  The set includes all possible
        combinations of valid index ranks, input tensor dtypes, and settings
        for parameters that are not declared with arg_unchecked in the schema.
        Also generates some invalid configurations.  Checks that opcheck will
        pass the valid ones, and log the appropriate errors for the invalid
        ones. 

        It can be thought of as a systematic 'sampling procedure' from a
        generative model with a set of hidden variables (index dims and ranks,
        and others) and observed variables (values of arguments)
        """
        success_list = []
        for config in fgraph.gen_graph_iterate(self.gen_graph.values()):
            arg_dict = { p: config.get(k, None) for p,k in self.params.items() }
            success_list.append(arg_dict)

        rank_mutations = []
        shape_kinds = (Kind.DATA_TENSOR, Kind.SHAPE_LIST, Kind.SHAPE_INT,
                Kind.SHAPE_TENSOR, Kind.SHAPE_TENSOR2D) 
        shape_args = [ (n, kind(kn)) for n, kn in self.params.items() if kn is
                not None and kind(kn) in shape_kinds ]

        def get_ranks(args):
            return tuple(mutations.get_rank(args[n], k) for n,k in shape_args)

        success_ranks = set(get_ranks(args) for args in success_list)
        for args in success_list:
            ranks = get_ranks(args)
            for i in range(len(ranks)):
                arg_name, arg_kind = shape_args[i]
                val = args[arg_name]
                aug_ranks = tuple(r+int(i == j) for j,r in enumerate(ranks)) 
                if aug_ranks in success_ranks:
                    continue
                else:
                    val_aug = mutations.increase_rank(val, arg_kind)
                    new_args = dict(args)
                    new_args[arg_name] = val_aug
                    rank_mutations.append(new_args)

                del_ranks = tuple(r-int(i == j) for j,r in enumerate(ranks))
                if del_ranks in success_ranks:
                    continue
                else:
                    val_del = mutations.decrease_rank(val, arg_kind)
                    new_args = dict(args)
                    new_args[arg_name] = val_del
                    rank_mutations.append(new_args)

        def gen_tensors(arg_dict):
            new_dict = {}
            for name, val in arg_dict.items():
                kname = self.params[name]
                if kname is not None and kind(kname) == Kind.DATA_TENSOR:
                    val = ge.from_stub(val)
                new_dict[name] = val
            return new_dict
        
        for arg_dict in success_list + rank_mutations:
            arg_dict = gen_tensors(arg_dict)

            # self.wrapped_op(**arg_dict)
            # print(arg_dict.keys())
            try:
                self.wrapped_op(**arg_dict)
            except Exception as e:
                if isinstance(e, SchemaError):
                    raise e
                else:
                    pass
                    # traceback.print_exc()

            # msg, err = self._validation_report(config)
            # print(f'{msg}\n{err}')
            msg = self._brief_report(arg_dict)
            print(msg)

    def _passed(self):
        return (
                isinstance(self.input_status, Success) and
                isinstance(self.framework_status, Success)
                )

    def _brief_report(self, arg_dict):
        """
        Summarize the call arguments
        """
        reps = {}
        for name, val in arg_dict.items():
            kname = self.params[name]
            k = kind(kname) if kname is not None else None
            if k == Kind.DATA_TENSOR:
                rep = f'Tensor.shape({val.shape.as_list()}):{val.dtype.name}'
            elif k == Kind.SHAPE_TENSOR:
                rep = f'ShapeTensor({val.numpy().tolist()})'
            elif k == Kind.SHAPE_TENSOR2D:
                rep = f'Tensor2D({val.numpy().tolist()})'
            else:
                rep = repr(val)
            reps[name] = rep
        call_string = ', '.join(f'{n}={reps[n]}' for n in self.arguments.keys()) 
        input_msg = self.input_status.message(self)
        framework_msg = self.framework_status.message(self)
        return (f'Call: {call_string}\n'
                f'OpCheck: {input_msg}\n'
                f'Framework: {framework_msg}\n')

    def _validation_report(self, config):
        err = ''
        err += f'Input Status: {self.input_status.message(self)}\n'
        err += f'Framework Status: {self.framework_status.message(self)}\n'
        msg = ''
        dims = ', '.join(f'{k}:{v}' for k,v in config[Kind.DIMS].items())
        msg += f'\nIndexes: {dims}'
        dtypes = ', '.join(f'{k}:{v.name}' for k,v in config[Kind.DTYPES].items()) 
        msg += f'\nDTypes: ({dtypes})' 
        # sigs = ', '.join(f'{n}: {sig}' for n, sig in self.arg_sigs.items())
        # msg += f'\nShape Signatures: {sigs}'
        for kname, arg_val in config.items():
            if isinstance(arg_val, tf.Tensor):
                msg += f'\n{kname} shape: {arg_val.shape.as_list()}'
            elif kpfx(kname) == '':
                continue
            else:
                msg += f'\n{kname}: {arg_val}'
        return msg, err

    def _report(self):
        print('Validation Report')

    # for debugging
    def _print_pred_graph(self):
        for n in self.input_pred_graph:
            print(f'{n.name}: {n.cached_val}')

    def _sig_inds(self, sig):
        inds = list(self.index.keys())
        return tuple(inds.index(idx) for idx in sig)

    def _check_arg_name(self, arg_name):
        """Ensure {arg_name} is a valid argument name"""
        if arg_name not in self.params:
            raise SchemaError(
                f'\'{arg_name}\' not a known parameter. '
                f'Known parameters are: {self.params.keys()}')

    def _get_arg(self, arg_name, default=None):
        """Retrieve the value of {arg_name} argument at call-time."""
        self._check_arg_name(arg_name)
        return self.arguments[arg_name]

    def _get_arg_kind(self, arg_name):
        """Retrieve the type expected for {arg_name}"""
        self._check_arg_name(arg_name)
        kname = self.params[arg_name]
        return base.kind(kname)

    def _set_arg_kname(self, arg_name, arg_kname):
        """
        Expect {arg_name} to have type {arg_kname}
        """
        if arg_name not in self.params:
            raise SchemaError(
                f'{type(self).__name__}: Attempted to add {arg_name} parameter '
                f'but it is not found in the framework op parameters. '
                f'Valid parameters are: {self.params.keys()}')
        
        if self.params[arg_name] is not None:
            raise SchemaError(
                f'{type(self).__name__}: Attempting to add {arg_name} as kname '
                f'{arg_kname} to the registry, but it is already registered '
                f'as type {self.params[arg_name].__name__}')
        self.params[arg_name] = arg_kname

    def _check_sig(self, signature, name):
        if any(s not in self.index.keys() for s in signature):
            raise SchemaError(
                f'Signature "{signature}" associated with \'{name}\' '
                f'contains one or more unregistered indices. '
                f'Current known indices are: '
                f"{','.join(self.index.keys())}"
                f'Call OpSchema.add_index with the missing index.')

    def _get_return(self, idx):
        try:
            return self.returns[idx]
        except IndexError:
            raise SchemaError(
                f'{type(self).__qualname__}({idx}) called but only '
                f'{len(self.returns)} returns')
    
    @staticmethod
    def _resolve_arg_names(caller, graph_cls, arg_names):
        """
        Find the unique arg_kname for each arg_name.  arg_name is either a
        simple string prefix, or a kname.  If a prefix,
        search for a unique kname in the registry with that prefix.  If a
        kname, take the name as-is
        """
        # find the unique arg_kname for each arg_name.  it is an error if it is
        # not unique
        knames = []
        all_knames = graph_cls.registry.keys()
        for arg_name in arg_names:
            if arg_name in all_knames:
                candidates = [arg_name]
            else:
                candidates = [ k for k in all_knames if kpfx(k) == arg_name ]
            if len(candidates) != 1:
                raise SchemaError(
                    f'{type(caller).__qualname__}: argument name \'{arg_name}\''
                    f' must identify a node with \':arg\' suffix or be a fully '
                    f'qualified kname')
            knames.append(candidates[0])
        return tuple(knames)

    def _init_pred_graph(self):
        P.clear_registry()
        P.add_node(Kind.SCHEMA, pr.Closure((True, self)))
        P.add_node(Kind.DTYPES, pr.ValidDTypes(self.dtype_cons))
        P.add_node(Kind.SIG_SHAPE_MAP, pr.SigShape())
        P.add_node(Kind.RANKS, pr.IndexRanks(self, self.rank_cons),
                Kind.SIG_SHAPE_MAP)
        P.add_node(Kind.IDIMS, pr.InputDims(), Kind.RANKS, Kind.SIG_SHAPE_MAP)
        P.add_node(Kind.CDIMS, pr.ComputedDims(self.comp_dims), Kind.IDIMS)

    def _init_gen_graph(self):
        G.clear_registry()
        G.add_node(Kind.SCHEMA, ge.Closure([self]))
        G.add_node(Kind.DTYPES, ge.DTypes(self.dtype_cons)) 
        G.add_node(Kind.RANKS, ge.Ranks(self, self.rank_cons)) 
        dims_obj = ge.IndexDimsGD(self.comp_dims, 1e5, 2e5)
        G.add_node(Kind.DIMS, dims_obj, Kind.RANKS)
        sig_inst_gobj = ge.SigInstantiation()
        G.add_node(Kind.SIG_INST, sig_inst_gobj, Kind.RANKS)

    def _add_pred_graph(self):
        pred_nodes = dict(P.registry)
        def is_return(node):
            ret_knames = (Kind.RETURN_TENSOR, Kind.VALID_RETURN)
            return base.kind(node.name) in ret_knames 

        self.input_pred_graph = { name: nd for name, nd in pred_nodes.items()
                if not is_return(nd) }

        self.return_pred_graph = { name: nd for name, nd in pred_nodes.items()
                if is_return(nd) }

    def _add_gen_graph(self):
        self.gen_graph = dict(G.registry)

    def _validate_constraints(self):
        """
        Called at the end of schema construction to check that schema
        constraints are self-consistent 
        """
        # Ensure that every tensor has exactly one dtype constraint
        for arg_name, arg_kname in self.params.items():
            if arg_kname is None:
                raise SchemaError(
                    f'{type(self).__qualname__}: \'{self.op_path}\' argument '
                    f'\'{arg_name}\' has no registered kname.  '
                    f'To ignore it, call arg_unchecked(\'{arg_name}\')')
                continue
            if base.kind(arg_kname) != Kind.DATA_TENSOR:
                continue
            if arg_name in self.dtype_cons.all(): 
                continue
            raise SchemaError(
                f'{type(self).__qualname__}: Error defining '
                f'\'{self.op_path}\' schema.  Tensor argument '
                f'\'{arg_name}\' has no registered dtype constraint.\n'
                f'Call tensor_equate_dtypes or tensor_valid_dtypes '
                f'for this tensor.')

        # add upper-bounds constraints for equated ranks
        for trg_sig, src_sig in self.rank_cons.equiv.items():
            limits = {}
            rank_maxs = self.rank_cons.maxs.items()
            for idx in src_sig:
                pair = next(((s,r) for s,r in rank_maxs if idx in s), None)
                if pair is None:
                    raise SchemaError(
                        f'{type(self).__qualname__}: Target signature '
                        f'\'{trg_sig}\' was equated with source signature '
                        f'\'{src_sig}\', but index \'{idx}\' in source '
                        f'signature has no limits registered with '
                        f'add_rank_limits.  All indices in source signature '
                        f'must appear in at least one add_rank_limits call.')
                max_sig, max_rank = pair
                limits[max_sig] = max_rank
            self.rank_cons.maxs[trg_sig] = sum(limits.values())

    @staticmethod
    def _convert_str(call_func, func_or_str, arg_names):
        if isinstance(func_or_str, str):
            if len(arg_names) != 0:
                raise SchemaError(
                    f'{type(call_func).__qualname__}: A string-valued '
                    f'arg \'{func_or_str}\' cannot have arguments. '
                    f'Got arguments \'{arg_names}\'')
            return lambda: func_or_str 
        else:
            return func_or_str

    # ============ PUBLIC API ====================
    def add_index(self, idx, description, min_rank=None, max_rank=None):
        """
        Add index {idx} with {description} to the schema.  {idx} must be a
        single letter and can be referred to in later signatures.

        If {min_rank} is provided, declare that the rank of this index be >=
        this value.

        If {max_rank} is provided, declare that the rank of this index be <=
        this value.
        """
        self.index[idx] = description
        if min_rank is not None or max_rank is not None:
            self.rank_cons.add_rank_limits(idx, min_rank, max_rank)

    def arg_rank(self, arg_name, sig):
        """
        Register {arg_name} to be an integer argument which defines the rank of
        {sig}
        """
        self.rank_cons.add_arg_rank(arg_name, sig)
        arg_kname = base.kname(arg_name, Kind.ARG)
        self._set_arg_kname(arg_name, arg_kname)
        P.add_node(arg_kname, pr.ArgInt(arg_name, 0, None), Kind.SCHEMA) 
        G.add_node(arg_kname, ge.Rank(sig), Kind.RANKS)
        rank_node = P.get_node(Kind.RANKS)
        rank_node.maybe_append_parent(arg_kname)
        rank_node.maybe_append_parent(Kind.SCHEMA)

    def rank_constraint(self, constraint_name, sig, rank_func, *arg_names):
        """
        Constrain the rank of signature {sig} to the value of
        rank_func(*arg_vals).

        {constraint_name} will be used in error messages issued by OpCheck.

        arg_vals are the resolved values of {arg_names}.
        """
        arg_knames = self._resolve_arg_names(self, P, arg_names)
        self.rank_cons.add_sig_func(constraint_name, sig, rank_func, arg_knames)
        rank_node = P.get_node(Kind.RANKS)
        for arg_kname in arg_knames:
            rank_node.maybe_append_parent(arg_kname)

    def arg_unchecked(self, arg_name):
        """
        Declare {arg_name} to be an argument unchecked by OpCheck 
        """
        self._set_arg_kname(arg_name, Kind.NONE)

    def computed_index(self, index, comp_func, *comp_arg_names):
        """
        Register {comp_func} to compute the dimensions of {index}.
        Will be called as: comp_func(*comp_arg_vals)

        {comp_arg_names} 
        """
        comp_knames = self._resolve_arg_names(self, P, comp_arg_names)
        self.comp_dims.add(index, comp_func, comp_knames)
        cdims_pnode = P.get_node(Kind.CDIMS)
        dims_gnode = G.get_node(Kind.DIMS)
        for kname in comp_knames:
            cdims_pnode.maybe_append_parent(kname)
            if kname != Kind.IDIMS:
                dims_gnode.maybe_append_parent(kname)

    def equate_ranks(self, target_index, source_index):
        """
        Declare that the rank of {target_index} be equal to {source_index}.
        It is required that all indices in {source_index} appear in some
        signature in a limit_ranks call.
        """
        if target_index not in self.index:
            raise SchemaError(
                f'{type(self).__qualname__}: target_index \'{target_index}\''
                f'is not a registered index')
        if (self.rank_cons.index_limited(target_index) or
                self.rank_cons.index_equated(target_index)):
            raise SchemaError(
                f'{type(self).__qualname__}: target index \'{target_index}\''
                f'is already registered as constrained')
        if not self.rank_cons.index_limited(source_index):
            raise SchemaError(
                f'{type(self).__qualname__}: source index \'{source_index}\''
                f'is not constrained with limit_ranks')
        self.rank_cons.equate_ranks(target_index, source_index)

    def limit_ranks(self, sig, min_val, max_val):
        """
        Declare that the rank of {sig} be in [{min_val}, {max_val}]
        """
        self._check_sig(sig, 'rank limits')
        self.rank_cons.add_rank_limits(sig, min_val, max_val)

    def valid_dtypes(self, tensor_name, type_list):
        """
        Declare that {tensor_name} can have any of the dtype strings in
        {type_list}.  Names in {type_list} are converted via
        tf.dtypes.as_dtype(name).  e.g. names like 'int32', 'int64', 'float32'
        """
        dtype_node = P.maybe_get_node(base.kname(tensor_name, Kind.DTYPE))
        if dtype_node is None:
            raise SchemaError(
                f'{type(self).__qualname__}: Parameter \'{tensor_name}\' is '
                f'not registered as a tensor')
        if tensor_name in self.dtype_cons.valid:
            raise SchemaError(
                f'{self.__qualname__}: Tensor \'{tensor_name}\' is already '
                f'registered with valid dtypes')

        dtypes = []
        for t in type_list:
            try:
                dt = tf.dtypes.as_dtype(t)
                dtypes.append(dt)
            except TypeError:
                raise SchemaError(
                    f'{type(self).__qualname__}: Type string \'{t}\' is not '
                    f'a valid tf.dtype representation')
        self.dtype_cons.add_valid(tensor_name, dtypes)

    def equate_dtypes(self, trg_tensor, src_tensor):
        """
        Declare that {trg_tensor} have the same dtype as {src_tensor}.
        Must first call arg_valid_dtypes(src_tensor, ...).
        trg_tensor must not be called in arg_valid_dtypes if it is called
        here.
        """
        src_node = P.maybe_get_node(base.kname(src_tensor, Kind.DTYPE))
        trg_node = P.maybe_get_node(base.kname(trg_tensor, Kind.DTYPE))
        if src_node is None or trg_node is None:
            raise SchemaError(
                f'{type(self).__name__}: Can only be called on two tensors. '
                f'Parameters \'{src_tensor}\' and \'{trg_tensor}\' are not '
                f'both tensors.')
        if src_tensor not in self.dtype_cons.valid:
            raise SchemaError(
                f'{self.__qualname__}: Must first register valid types for '
                f'src_tensor (\'{src_tensor}\'')
        if trg_tensor in self.dtype_cons.all():
            raise SchemaError(
                f'{self.__qualname__}: trg_tensor (\'{trg_tensor}\') '
                f'already has a dtype constraint')
        self.dtype_cons.add_equiv(trg_tensor, src_tensor)

    def arg_int(self, arg_name, lo=None, hi=None):
        """
        Declare {arg_name} to be an integer that can take on values in a range.
        If {lo} is None, it is sys.maxint
        If {hi} is None, it is -sys.maxint-1 
        """
        arg_kname = base.kname(arg_name, Kind.ARG)
        self._set_arg_kname(arg_name, arg_kname)
        pred_obj = pr.ArgInt(arg_name, lo, hi)
        gen_obj = ge.Int(lo, hi)
        P.add_node(arg_kname, pred_obj, Kind.SCHEMA)
        G.add_node(arg_kname, gen_obj)

    def _arg_pseudo(self, pseudo_name, pred_func, gen_func, arg_name):
        """
        Creates a pseudo-input argument called {pseudo_name}, which is used to
        break a dependency cycle in nodes of the Generation Graph or Predicate
        graph.

        {gen_func}() generates all legal values for the pseudo argument during
        the schema validation phase.

        {pred_func}(arg_val) returns a derived value which represents the
        pseudo-input's value.  It is as if that value were provided directly to
        the framework operation.
        """
        arg_kname = base.kname(pseudo_name, Kind.PSEUDO)
        pfunc_obj = pr.ArgFunc(arg_name, pred_func)
        P.add_node(arg_kname, pfunc_obj, Kind.SCHEMA) 
        G.add_node(arg_kname, gen_func)

    def _arg_func(self, arg_name, arg_kind, pred_func, gen_func,
            *func_arg_names):
        """
        Register {arg_name} to be validated with the call
        pred_func(arg_val, *func_arg_vals).  (Note the first argument is the
        supplied schema)

        For testing, generate values with a call to gen_func(*func_arg_vals).

        pred_func must return tuples of either:
        True, <value>
        False, SchemaError
        """
        knames = self._resolve_arg_names(self, P, func_arg_names)
        arg_kname = base.kname(arg_name, arg_kind)
        self._set_arg_kname(arg_name, arg_kname)
        pfunc_obj = pr.ArgFunc(arg_name, pred_func)
        P.add_node(arg_kname, pfunc_obj, Kind.SCHEMA, *knames)
        G.add_node(arg_kname, gen_func, *knames)

    def arg_option(self, arg_name, options):
        """
        Expect {arg_name} to take on one of the values in {options}
        """
        try:
            iter(options)
        except TypeError:
            raise SchemaError(
                f'{type(self).__qualname__}: \'options\' argument must be '
                f'iterable.  Got {type(options)}')
        def options_gen():
            return options

        arg_kname = base.kname(arg_name, Kind.ARG)
        self._set_arg_kname(arg_name, arg_kname)
        G.add_node(arg_kname, options_gen)
        def options_pred(arg_val):
            if arg_val in options:
                return True, arg_val
            else:
                return False, NonOptionError(arg_name, arg_val) 
        pred_obj = pr.ArgFunc(arg_name, options_pred)
        P.add_node(arg_kname, pred_obj, Kind.SCHEMA)

    def arg_layout(self, arg_name, layouts, rank_idx):
        """
        Declares {arg_name} to control layout-dependent signatures for tensors. 

        {layouts} is an array, where each element is a map of: rank => code

        The rank of {rank_idx} determines which layout is mapped.
        """
        # Define the pseudo-arg
        self.num_layouts = len(layouts)
        pseudo_gen = ge.Layout(self.num_layouts)
        pseudo_pred = pr.ArgLayout(arg_name, layouts)
        self._arg_pseudo('layout', pseudo_pred, pseudo_gen, arg_name)
        pseudo_kname = kname('layout', Kind.PSEUDO)

        # define the real arg 
        arg_pred = pr.ArgDataFormat(arg_name, layouts, rank_idx)
        arg_gen = ge.DataFormat(layouts, rank_idx)
        self._arg_func(arg_name, Kind.LAYOUT, arg_pred, arg_gen, Kind.RANKS,
                pseudo_kname)

        # inform the sig_instantiation node
        inst_gnode = G.get_node(Kind.SIG_INST)
        layout_kname = kname(arg_name, Kind.LAYOUT)
        layout_node = G.get_node(layout_kname)
        inst_gnode.append_parent(layout_node)

    def arg_tensor(self, arg_name, *sigs):
        """
        Register {arg_name} as a tensor.  

        sigs are all strings of signatures.  If len(sigs) == 1, then it
        specifies a static signature regardless of whether 'arg_layout' was
        called.  If len(sigs) > 1, then arg_layout is required to be called
        before this call.
        """
        # Creates Predicate nodes
        # arg_name:arg (tensor)
        # arg_name:shape (int list, the shape of the tensor)
        # arg_name:sig (str, the associated signature)
        # arg_name:dtype (tf.dtype, the tensor dtype)
        if len(sigs) != 1 and len(sigs) != self.num_layouts:
            raise SchemaError(
                f'{type(self).__qualname__}: There are {self.num_layouts} '
                f'layouts (as established by the call to \'arg_layout\') but '
                f'{len(sigs)} elements of \'*sigs\' argument.')

        arg_kname = kname(arg_name, Kind.DATA_TENSOR)
        self._set_arg_kname(arg_name, arg_kname)

        dtype_kname = kname(arg_name, Kind.DTYPE)
        shape_kname = kname(arg_name, Kind.SHAPE)
        sig_kname = kname(arg_name, Kind.SIG) 
        self.rank_cons.add_shape_sig(shape_kname, sig_kname)

        if len(sigs) == 1:
            sig_pobj = pr.Closure((True, sigs[0]))
            sig_gobj = ge.Closure([sigs[0]])
            layout = tuple()
        else:
            sig_pobj = pr.LayoutOption(arg_name, sigs)
            sig_gobj = ge.LayoutOption(sigs) 
            layout = (base.kname('layout', Kind.PSEUDO),)

        arg_pobj = pr.ArgType(arg_name, tf.Tensor) 
        P.add_node(arg_kname, arg_pobj, Kind.SCHEMA)
        dtype_pnode = P.add_node(dtype_kname, pr.dtype, arg_kname)
        shape_pnode = P.add_node(shape_kname, pr.tensor_shape, arg_kname)
        sig_pnode = P.add_node(sig_kname, sig_pobj, *layout)

        arg_gobj = ge.TensorStub(arg_name)
        sig_gnode = G.add_node(sig_kname, sig_gobj, *layout)
        G.add_node(arg_kname, arg_gobj, sig_kname, Kind.DIMS, Kind.DTYPES) 
        dims_gnode = G.get_node(Kind.DIMS)
        dims_gnode.append_parent(sig_gnode)

        inst_gnode = G.get_node(Kind.SIG_INST)
        inst_gnode.append_parent(sig_gnode)

        sigshape_pnode = P.get_node(Kind.SIG_SHAPE_MAP)
        sigshape_pnode.append_parent(sig_pnode)
        sigshape_pnode.append_parent(shape_pnode)

        # Create edges
        dtypes_pnode = P.get_node(Kind.DTYPES)
        ranks_pnode = P.get_node(Kind.RANKS)
        idims_pnode = P.get_node(Kind.IDIMS)
        dtypes_pnode.append_parent(dtype_pnode)
        ranks_pnode.append_parent(shape_pnode)
        ranks_pnode.append_parent(sig_pnode)
        idims_pnode.append_parent(shape_pnode)
        idims_pnode.append_parent(sig_pnode)

    def _arg_shape_func(self, arg_name, sigs, _type, arg_kind, pred_obj, gen_obj):
        """
        Backend function for arg_shape_* API functions 
        """
        # TODO: add obj arguments
        if len(sigs) != 1 and len(sigs) != self.num_layouts:
            raise SchemaError(
                f'{type(self).__qualname__}: There are {self.num_layouts} '
                f'layouts (as established by the call to \'arg_layout\') but '
                f'{len(sigs)} elements of \'*sigs\' argument.')

        arg_kname = kname(arg_name, arg_kind)
        self._set_arg_kname(arg_name, arg_kname)
        arg_pobj = pr.ArgType(arg_name, _type)
        shape_pobj = pred_obj

        if len(sigs) == 1:
            sig_pobj = pr.Closure((True, sigs[0]))
            sig_gobj = ge.Closure([sigs[0]])
            layout = tuple()
        else:
            sig_pobj = pr.LayoutOption(arg_name, sigs)
            sig_gobj = ge.LayoutOption(sigs) 
            layout = (base.kname('layout', Kind.PSEUDO),)

        sig_kname = kname(arg_name, Kind.SIG)
        shp_kname = kname(arg_name, Kind.SHAPE)
        self.rank_cons.add_shape_sig(shp_kname, sig_kname)

        arg_pnode = P.add_node(arg_kname, arg_pobj, Kind.SCHEMA)
        shape_pnode = P.add_node(shp_kname, shape_pobj, arg_kname)
        sig_pnode = P.add_node(sig_kname, sig_pobj, *layout)

        sig_gnode = G.add_node(sig_kname, sig_gobj, *layout)
        G.add_node(arg_kname, gen_obj, Kind.DIMS, sig_kname)
        dims_gnode = G.get_node(Kind.DIMS)
        dims_gnode.append_parent(sig_gnode)

        inst_gnode = G.get_node(Kind.SIG_INST)
        inst_gnode.append_parent(sig_gnode)

        sigshape_pnode = P.get_node(Kind.SIG_SHAPE_MAP)
        sigshape_pnode.append_parent(sig_pnode)
        sigshape_pnode.append_parent(shape_pnode)

    def arg_shape_list(self, arg_name, *sigs):
        """
        Register {arg_name} as an integer list parameter which defines the
        shape of a signature.  
        """
        # Creates nodes:
        # arg_name:arg (int list)
        # arg_name:shape (int list, the same value as :arg)
        # arg_name:sig (str, the associated signature)
        pred_obj = pr.ShapeList(arg_name)
        gen_obj = ge.ShapeList()
        return self._arg_shape_func(arg_name, sigs, list, Kind.SHAPE_LIST,
                pred_obj, gen_obj)

    def arg_shape_int(self, arg_name, index):
        """
        Register {arg_name} as an integer parameter which defines the shape of
        an index.  The shape will be the broadcasted value of the argument if
        the index has rank greater than 1.

        This is the only arg_shape_* API function which does not define the
        rank of the index. 
        """
        # TODO: currently uses arg_shape_func, which assumes the arg defines
        # the rank.  In this case, it only defines the shape as the integer 
        # value broadcasted {rank} times.  But, the rank is not determined from
        # this input
        pred_obj = pr.ShapeInt(arg_name)
        gen_obj = ge.ShapeInt()
        return self._arg_shape_func(arg_name, (index,), int, Kind.SHAPE_INT,
                pred_obj, gen_obj) 

    def arg_shape_tensor(self, arg_name, *sigs):
        """
        Register {arg_name} as a 1D integer tensor whose elements define the
        shape of a signature.  
        """
        # Creates nodes:
        # arg_name:arg (tensor)
        # arg_name:shape (int list, the contents of the tensor)
        # arg_name:sig (str, the associated signature)
        # (no dtype node is created)
        pred_obj = pr.ShapeTensor(arg_name)
        gen_obj = ge.ShapeTensor()
        return self._arg_shape_func(arg_name, sigs, tf.Tensor,
                Kind.SHAPE_TENSOR, pred_obj, gen_obj)

    def arg_shape_tensor2d(self, arg_name, *sigs):
        """
        Register {arg_name} as a 2D integer tensor 'ten' defining the shape of
        sigs.  

        In the single layout case, sigs[i] are strings, and ten[d,i]
        defines dims(sigs[i])[d].  

        In the multiple layout case, sigs[i][l] is the i'th signature for
        layout l, and ten[d,i] defines dims(sigs[i][l])[d]

        Examples:

        Single layout case:

        ten = [ [1,2], [3,4], [5,6] ]
        sigs = ('b', 'e')
        
        defines 
        dims('b') := [1,3,5]
        dims('e') := [2,4,6]

        Multiple layout case:

        ten = [ [1,2], [3,4], [5,6] ]
        sigs = (('b', 'e'), ('e', 'b'))

        defines:
        layout 0: dims('b') = [1,3,5], dims('e') = [2,4,6] 
        layout 1: dims('b') = [2,4,6], dims('b') = [1,3,5]
        """
        # Creates nodes:
        # arg_name:arg (the 2D tensor)
        # arg_name.i:shape (the i'th shape)
        # arg_name.i:sig (the i'th signature)
        arg_kname = kname(arg_name, Kind.SHAPE_TENSOR2D)
        self._set_arg_kname(arg_name, arg_kname)
        arg_pobj = pr.ArgType(arg_name, tf.Tensor)
        arg_gobj = ge.ShapeTensor2D()
        P.add_node(arg_kname, arg_pobj, Kind.SCHEMA)
        idims_pnode = P.get_node(Kind.IDIMS)
        ranks_pnode = P.get_node(Kind.RANKS)
        ten_gnode = G.add_node(arg_kname, arg_gobj, Kind.DIMS)
        layout_kname = base.kname('layout', Kind.PSEUDO)

        for i, sig in enumerate(sigs):
            prefix = f'{arg_name}.{i}'
            sig_kname = kname(prefix, Kind.SIG)
            shp_kname = kname(prefix, Kind.SHAPE)
            self.rank_cons.add_shape_sig(shp_kname, sig_kname)
            shp_pobj = pr.ShapeTensorSlice(arg_name, i)
            if isinstance(sig, str):
                sig_gobj = ge.Closure(list(sig))
                sig_pobj = pr.Closure((True, sig))
                sig_pnode = P.add_node(sig_kname, sig_pobj)
                sig_gnode = G.add_node(sig_kname, sig_gobj) 
            else:
                sig_pobj = pr.LayoutOption(arg_name, list(sig))
                sig_gobj = ge.LayoutOption(list(sig))
                sig_pnode = P.add_node(sig_kname, sig_pobj, layout_kname)
                sig_gnode = G.add_node(sig_kname, sig_gobj, layout_kname) 

            shp_pnode = P.add_node(shp_kname, shp_pobj, arg_kname)
            idims_pnode.append_parent(shp_pnode)
            idims_pnode.append_parent(sig_pnode)
            ranks_pnode.append_parent(shp_pnode)
            ranks_pnode.append_parent(sig_pnode)
            ten_gnode.append_parent(sig_gnode)

    def add_index_predicate(self, pred_name, status_func, index_combo):
        """
        Registers {status_func} with the schema to be used as an additional
        predicate for {indexes} dimensions.

        {pred_name} is a name given to this custom predicate.  It may be used
        in error messages.

        Called as status_func(*index_shapes), where index_shapes are the
        resolved shapes of each index, in order, in {index_combo}.  They are
        provided as numpy arrays.

        status_func must return an instance of SchemaStatus

        Custom status functions are found in the flib module.
        """
        pobj = pr.Dims(pred_name, status_func, index_combo)
        dims_kname = kname(pred_name, Kind.NONE)
        dims_pnode = P.add_node(dims_kname, pobj, Kind.IDIMS, Kind.CDIMS)

    def add_index_generator(self, gen_func, output_indices, input_indices, 
            *gen_args):
        """
        Registers {gen_func_obj} with the schema to be used to generate
        dimension combinations for {output_indices}, which is a string
        consisting of individual index one-letter codes.

        It is called as gen_func(input_ranks, *gen_args) and returns a list of
        shape tuples.  The shapes in each shape tuple correspond with the
        indices in output_indices.

        input_ranks are the resolved ranks of input_indices

        Custom generator function objects are found in the flib module.
        """
        dims_kname = kname(output_indices, Kind.DIMS)
        gobj = ge.Dims(gen_func, output_indices, input_indices, gen_args)
        gen_dims_gnode = G.add_node(dims_kname, gobj, Kind.RANKS)
        dims_gnode = G.get_node(Kind.DIMS)
        dims_gnode.append_parent(gen_dims_gnode)

    def return_tensor(self, *sigs):
        """
        Append a return tensor to the list of expected return tensors.

        *sigs may contain either one element, or {num_layout} elements.  If one
        element, it defines the static signature for the return tensor.  If
        multiple, they are defined by the provided layout as declared in
        'arg_layout'
        """
        if len(sigs) != 1 and len(sigs) != self.num_layouts:
            raise SchemaError(
                f'{type(self).__qualname__}: There are {self.num_layouts} '
                f'layouts (as established by the call to \'arg_layout\') but '
                f'{len(sigs)} elements of \'*sigs\' argument.')
        index = self.num_returns
        ret_name = str(index)
        ret_kname = kname(ret_name, Kind.RETURN_TENSOR)
        valid_return = kname(ret_name, Kind.VALID_RETURN)
        pshape_kname = kname(ret_name, Kind.PSHAPE)
        sig_kname = kname(ret_name, Kind.SIG)
        if len(sigs) == 1:
            sig_pobj = pr.Closure((True, sigs[0]))
            sig_gobj = ge.Closure([sigs[0]])
            layout = tuple()
        else:
            sig_pobj = pr.LayoutOption(str(index), sigs)
            sig_gobj = ge.LayoutOption(sigs) 
            layout = (base.kname('layout', Kind.PSEUDO),)

        rten_pobj = pr.GetReturnTensor(index)
        rvalid_pobj = pr.ValidReturnShape(index)
        P.add_node(ret_kname, rten_pobj, Kind.SCHEMA)
        P.add_node(sig_kname, sig_pobj, *layout)  
        P.add_node(pshape_kname, pr.predicted_shape, Kind.IDIMS, Kind.CDIMS,
                sig_kname)
        P.add_node(valid_return, rvalid_pobj, ret_kname, pshape_kname) 
        sig_gnode = G.add_node(sig_kname, sig_gobj, *layout) 
        dims_gnode = G.get_node(Kind.DIMS)
        dims_gnode.append_parent(sig_gnode)
        self.num_returns += 1

        inst_gnode = G.get_node(Kind.SIG_INST)
        inst_gnode.append_parent(sig_gnode)

