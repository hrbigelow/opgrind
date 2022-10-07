import traceback
import inspect
import os, io, sys
import re
import tensorflow as tf
import numpy as np
import itertools
import copy
from collections import defaultdict, OrderedDict
from . import predicates as pr
from . import generators as ge
from . import base
from .redirect import stderr_redirector
from . import fgraph
from . import flib
from .error import *
from .fgraph import PredNode as P, GenNode as G, FuncNode as F

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
class _TestResult(object):
    """
    One schema test result.  Holds all sufficient information about the test.
    Provides convenient reporting and classification functions
    """
    def __init__(self, op, _id, config):
        self.op = op
        self.id = _id
        self.config = config
        expect_status = fgraph.node_name(ge.StatusAggregator)
        statuses = self.config[expect_status]
        err = list(s for s in statuses if s != Success)
        if len(err) > 1:
            stat = None
        else:
            stat = err[0] if len(err) > 0 else Success
        self.expect_class = stat

    def add_result(self):
        self.opcheck_status = self.op.input_status
        if isinstance(self.op.framework_status, Success):
            self.framework_status = self.op.framework_status
        else:
            self.framework_status = self.op.framework_status.ex

        ex_stat_cls = self.expect_class
        op_stat = self.opcheck_status
        fr_stat = self.framework_status
        if type(op_stat) != ex_stat_cls:
            cat = 'FAIL'
        else:
            op_neg = isinstance(op_stat, Success)
            fr_neg = isinstance(fr_stat, Success)
            if op_neg:
                cat = 'TN' if fr_neg else 'FN'
            else:
                cat = 'FP' if fr_neg else 'TP'
        self.category = cat

    def make_args(self):
        # create arguments 
        d = { p: self.config.get(node.name, None) for p,node in
                self.op.params.items() if node is not None }
        arg_dict = {}
        for name, val in d.items():
            node = self.op.params[name]
            if type(node.func) == ge.TensorStub:
                val = ge.from_stub(val)
            arg_dict[name] = val
        return arg_dict

    def stat_keys(self):
        # return an ordered set of keys
        keys = ['ID', 'CATEGORY', 'EXPECT_STATUS', 'OPCHECK_STATUS',
                'FRAMEWORK_STATUS', 'RANKS']
        keys.extend(self.op.params.keys())
        return keys

    def stats(self):
        # retrieve sufficient statistics to determine the kind of test
        stats = []
        keys = self.stat_keys()
        ranks = self.config[fgraph.node_name(ge.GetRanks)]

        stats = [ 
                str(self.id), 
                self.category, 
                self.expect_class.__name__,
                self.opcheck_status.__class__.__name__,
                self.framework_status.__class__.__name__,
                ','.join(str(r) for r in ranks.values())
                ]

        arg_shapes = self.config[fgraph.node_name(ge.GetArgShapes)]
        arg_dtypes = self.config[fgraph.node_name(ge.GetDTypes)]

        for arg, kn in self.op.params.items():
            # any shape-related args are specially formatted
            if arg in arg_shapes:
                rep = str(arg_shapes[arg])
                if arg in arg_dtypes:
                    rep += ':' + arg_dtypes[arg].name 
            else:
                rep = self.config.get(kn, '')
            stats.append(rep)

        return stats

    def run(self):
        # run the op and store the results
        string_err = io.BytesIO()
        arg_dict = self.make_args()

        try:
            with stderr_redirector(string_err):
                self.op.wrapped_op(**arg_dict)
        except OpCheckInternalError as e:
            print(string_err.getvalue().decode('UTF-8'))
            raise e
        except BaseException as e:
            # this should always be from TensorFlow
            trace = inspect.trace()
            for frame in reversed(trace):
                mod = inspect.getmodule(frame[0])
                if mod is not None:
                    break
            modname = mod.__name__
            # print(modname, flush=True)
            if modname.split('.')[0] == 'tensorflow':
                # print('exception inside tensorflow:')
                # traceback.print_stack()
                pass
            else:
                assert False, 'exception outside tf should not be possible'
                print('exception outside of tensorflow')
                traceback.print_stack()
                raise e
        self.add_result()

    def report(self):
        """
        Generate a human-readable report
        """
        cat = self.category
        op_stat = self.opcheck_status
        op_msg = op_stat.message(self.op)
        fr_msg = self.op.framework_status.message(self.op)
        if cat == 'TP':
            return f'Framework\n{fr_msg}\nOpCheck\n{op_msg}\n'
        elif cat == 'TN':
            return ''
        elif cat == 'FP':
            return f'OpCheck\n{op_msg}\n'
        else:
            return f'Framework\n{fr_msg}\n'

        
class SchemaApi(object):
    def __init__(self, op_path):
        self.op_path = op_path
        self.index = {} # idx => description

        # params is used to retrieve values during testing
        self.params = None # ordered dict of param_name => GenNode
        self.gen_graph = {}
        self.inv_graph = {} # name => node
        self.pred_graph = {}
        self.return_pred_graph = {}
        self.data_formats = base.DataFormats()
        self.rank_candidates = base.RankCandidates(self)
        self.rank_cons = [] 
        self.dtype_cons = base.DTypeConstraints()
        self.dims_graph = base.CompDimsGraph()
        self.gen_indices = base.GenIndices()
        self.comp_dims_templates = {} # idx => PredNode with TemplateFunc
        self.num_returns = 0

        # error status
        self.input_status = None
        self.framework_status = None

        # call time values
        self.arguments = {}
        self.returns = []

    def _gen_node(self, gen_class, name=None):
        name = fgraph.node_name(gen_class, name)
        return self.gen_graph.get(name, None)

    def _pred_node(self, pred_class, name=None):
        name = fgraph.node_name(pred_class, name)
        return self.pred_graph.get(name, None)

    def _inv_node(self, gen_class, name=None):
        name = fgraph.node_name(gen_class, name)
        return self.inv_graph.get(name, None)

    def _init_schema(self, func_sig, init_schema_func):
        # edges to create for the pred graph
        self.pending_pred_edges = {} # node name -> [parent node name, ...]
        self.pending_index_edges = {} # node name -> [idx, idx, ...]
        self.func_sig = func_sig
        pars = func_sig.parameters.keys()
        self.params = OrderedDict({ k: None for k in pars })
        self._init_pred_graph()
        self._init_gen_graph()
        init_schema_func(self)
        self._add_pred_graph()
        self._add_inv_graph()
        self._finalize()

    def _prepare_call(self, *args, **kwargs):
        """Call during the framework call phase"""
        bind = self.func_sig.bind(*args, **kwargs)
        bind.apply_defaults()
        self.arguments = bind.arguments
        self.returns.clear()
        self.input_status = None
        self.framework_status = None

    def _check_args(self):
        """
        The main function to check all input arguments for all constraints
        registered on the schema
        """
        error = fgraph.pred_graph_evaluate(self.pred_graph.values())
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

    def _ranks_sigs_format(self):
        """
        Generates all valid combinations of ranks_map, sigs_map, and
        data_format as ordered tuples
        """
        ranks = self._inv_node(pr.Ranks)
        sigs = self._inv_node(pr.SigMap)
        data_format = self._inv_node(pr.DataFormat)
        gen = fgraph.all_values(ranks, sigs, data_format)
        tups = list(gen)
        return tups

    def _shape_key_order(self, shape_keys):
        # order the shape keys in argument order
        arg_order = list(self.params.keys())
        arg_order.extend(f'return[{i}]' for i in range(self.num_returns))

        def key_fun(shape_key):
            pfx = shape_key.split('.')[0]
            return arg_order.index(pfx)
        key_order = sorted(shape_keys, key=key_fun)
        return key_order

    # TODO: fix headers (possibly integrate a header function in a NodeFunc base
    # class?)
    def _inventory(self):
        """
        Generate a usage inventory for the op.  Includes all combinations of
        input signatures, data format, dtypes
        """
        ranks = self._inv_node(ge.Ranks)
        sigs = self._inv_node(ge.SigMap)
        dtypes = self._inv_node(ge.ValidDTypes)
        data_format = self._inv_node(ge.DataFormat)
        gen = fgraph.all_values(ranks, sigs, dtypes, data_format)
        geometry = list(gen)
        sig_map = geometry[0][1]
        args = [ *sig_map ]
        if self.data_formats.configured:
            args.append(self.data_formats.arg_name)
        arg_order = self._shape_key_order(args)

        rows = [arg_order]
        for ranks, sigs, dtypes, cand_format in geometry:
            row = []
            for arg in arg_order:
                node = self.params.get(arg, None)
                func = None if node is None else node.func
                if isinstance(func, ge.DataFormat): 
                    row.append(cand_format)
                elif isinstance(func, ge.TensorStub):
                    sig = sigs[arg]
                    inst = ''.join(s * ranks[s] for s in sig)
                    row.append(inst)
                    dtype = dtypes[arg].name
                    row.append(dtype)
                else:
                    pass
            rows.append(row)

        table, _ = tabulate(rows, '  ', left_align=True)
        return table

    def _index_inventory(self):
        """
        Generate a formatted report of the indices with their rank constraints
        """
        rows = []
        rows.append(['Index', 'Description'])
        rows.extend([ix,desc] for ix,desc in self.index.items())
        tab, _ = tabulate(rows, '  ', left_align=True)
        return tab

    def _rank_error_report(self, shape_map, data_format, report):
        """
        This report is generated when the framework op arguments are such that
        no consistent set of index ranks could be inferred.  The report
        consists of a set of possible ways to fix the inputs.

        Each item is a table, followed by one or more text suggestions on how
        to fix the inputs.  The table has the following rows:

        arguments
        shapes
        interpretation
        errors

        'arguments' shows formatted argument names highlighting the relevant
        aspect of the argument.  (see api.py:_shape_header)

        DATA_TENSOR:  {arg_name}.shape
        SHAPE_TENSOR: {arg_name}.numpy()
        SHAPE_LIST: {arg_name}
        SHAPE_INT: {arg_name}

        'shapes' shows the actual submitted values of the argument aspect.
        All of these represent shapes to be applied to OpCheck indices.

        'interpretation' shows, for each component of a shape, the one-letter
        OpCheck index name which is inferred in this item.

        'errors' is an ASCII representation highlighting where the error
        occurred.
        
        The list of plain-text suggestions provides one way to fix the errors.
        It is necessarily a guess about what the user might have intended.
        ...

        """
        # need to augment this with another map of other argument values
        args = [ *shape_map ]
        if self.data_formats.arg_name is not None:
            args.append(self.data_formats.arg_name)
        arg_order = self._shape_key_order(args)
        cand_reports = []

        leader_col = [ 'arguments', 'shapes', 'interpretation', 'errors' ]

        for cand in report:
            # the sub_table is a map of arg_name => rows
            # the rows are: actual shape, signature instantiation, highlight
            # carats
            sub_table = {} 
            for n, shape in shape_map.items():
                shape_rank = len(shape)
                shape_row = [str(sz) for sz in shape]
                sub_table[n] = [shape_row]

                # populate the signature instantiation row
                sig = cand.sigs[n]
                inst_row = []
                for s in sig:
                    r = cand.ranks[s]
                    if r == 1:
                        inst_row.append(s)
                    else:
                        inst_row.extend(f'{s}{i+1}' for i in range(r))
                inst_rank = len(inst_row)
                sub_table[n].append(inst_row)

                # populate the highlight carat row
                pos = cand.highlight[n]
                num_components = max(shape_rank, inst_rank)
                highlight_row = []
                for c in range(num_components):
                    if c in pos:
                        w1 = len(shape_row[c]) if c < shape_rank else 0
                        w2 = len(inst_row[c]) if c < inst_rank else 0
                        carat = '^' * max(w1, w2)
                    else:
                        carat = ''
                    highlight_row.append(carat)
                sub_table[n].append(highlight_row)

            # format the sub-tables
            columns = {} 
            for name, tab in sub_table.items():
                tab = sub_table[name]
                shape_rows, _ = tabulate(tab, ' ', left_align=True)
                hdr = self._shape_header(name)
                col = [hdr, *shape_rows]
                columns[name] = col

            if self.data_formats.arg_name is not None:
                if (
                        (data_format == cand.format) or
                        (data_format is None and cand.format is None)
                        ):
                    hl = ''
                else:
                    hl = '^' * max(len(data_format), len(cand.format))
                columns[self.data_formats.arg_name] = [
                        self.data_formats.arg_name, 
                        data_format, 
                        cand.format,
                        hl
                        ]

            col_array = [leader_col] + [ columns[name] for name in arg_order ]
            main_table = np.array(col_array).transpose().tolist()
            main_rows, _ = tabulate(main_table, '   ', True)
            table = '\n'.join(main_rows)
            suggs = '\n'.join(cand.suggestions)
            cand_reports.append(f'{table}\n{suggs}\n')

        full_report = '\n'.join(cand_reports)
        return full_report

    def _index_usage_phrase(self, idx, component_usages, ranks):
        def phrase_join(names):
            qnames = [f'\'{n}\'' for n in names]
            phrase = ', '.join(qnames[:-1])
            sep = '' if phrase == '' else ' and '
            return sep.join((phrase, qnames[-1]))

        phrases = []
        r = ranks[idx]
        for c, usage in enumerate(component_usages):
            if len(usage) == 1:
                continue
            sep = ''
            main_phrase = ''
            for sz, arg_list in usage.items():
                phrase = phrase_join(arg_list)
                main_phrase += sep + f'size {sz} in {phrase}'
                sep = ', '
            idxc = idx if r == 1 else f'{idx}{c+1}'
            msg = f'Index \'{idxc}\' ({self.index[idx]}) has {main_phrase}.'
            phrases.append(msg)
        return '\n'.join(phrases)

    # compute the highlight mask from the component usage maps
    @staticmethod
    def _highlight_mask(ranks, sigs, shapes, idx_usage):
        highlight = defaultdict(list)
        for arg, sig in sigs.items():
            shape = shapes[arg]
            for idx in sig:
                comp = idx_usage.get(idx, None)
                if comp is None:
                    mask = [False] * ranks[idx]
                else:
                    mask = [ (len(c) != 1) for c in comp ]
                highlight[arg].extend(mask)
        return dict(highlight)

    def _index_diagram(self, highlight_map, ranks, sigs, shapes):
        arg_order = [ n for n in self.params.keys() if n in sigs.keys() ]
        dims = { n: [shp] for n, shp in shapes.items() }
        table_data = {} # arg => [shape, inst, highlight]
                        # shape is e.g.:     [15, 3, 10, 5]
                        # inst is e.g.       ['b', 'i1', 'i2', 'k']
                        # highlight is e.g.: ['', '', '^^', '']

        for arg, sig in sigs.items():
            shape = shapes[arg]
            mask = highlight_map[arg]
            table_data[arg] = [shape]
            inst = []
            highlight = []
            for idx in sig:
                if ranks[idx] == 1:
                    inst.append(idx)
                else:
                    inst.extend(f'{idx}{i+1}' for i in range(ranks[idx]))

            z = zip(mask, inst)
            hl = ['^' * len(i) if m else '' for m, i in z]
            highlight.extend(hl)
            table_data[arg].append(inst)
            table_data[arg].append(highlight)
        
        columns = []
        for arg, rows in table_data.items():
            fmt, _ = tabulate(rows, ' ', left_align=True)
            col = [arg, *fmt]
            columns.append(col)
        table = np.array(columns).transpose().tolist()
        fmt, _ = tabulate(table, '   ', left_align=True)
        return '\n'.join(fmt)

    def _index_usage_error(self, idx_usage, ranks, sigs, shapes):
        """
        Generate the message for an IndexUsageError.
        {idx_usage} is: idx => [ (dim => [arg1, ...]),
                                 (dim => [arg1, ...]),
                                 ...
                               ]
        {sigs} is: arg => sig
        {shapes} is: arg => shape
        """
        highlight_map = self._highlight_mask(ranks, sigs, shapes, idx_usage)
        diagram = self._index_diagram(highlight_map, ranks, sigs, shapes)

        index_msgs = []
        for idx, comp in idx_usage.items():
            phrase = self._index_usage_phrase(idx, comp, ranks)
            index_msgs.append(phrase)

        text = '\n'.join(index_msgs)
        return diagram + '\n' + text

    def _index_constraint_error(self, text, index_highlight, ranks, sigs,
            shapes):
        # compute the arg => mask from idx => mask
        arg_highlight = defaultdict(list)
        for arg, sig in sigs.items():
            for s in sig:
                mask = index_highlight.get(s, [False] * ranks[s])
                arg_highlight[arg].extend(mask)

        diagram = self._index_diagram(arg_highlight, ranks, sigs, shapes)
        return diagram + '\n' + text

    def _dtype_excluded_report(self, ten_names, ten_dtypes, rank_map, layout):
        """
        Generates an error report to the user indicating that The combination
        of {ten_names} having {ten_dtypes}, with the particular setting of
        index ranks given in {rank_map} and for the given layout is disallowed.

        If rank_map is empty, the dtype combination is disallowed for every
        rank combination, and ranks will not be mentioned in the report.

        If layout is None, the dtype combination is disallowed for every
        layout, and layout will not be mentioned in the report
        """
        header = ['configuration', 'value']
        table = [ header ]
        for n, d in zip(ten_names, ten_dtypes):
            row = [ f'tensor \'{n}\' dtype', d.name ]
            table.append(row)
        for idx, rank in rank_map.items():
            row = [ f'\'{self.index[idx]}\' # dims', rank ]
            table.append(row)
        if layout is not None:
            data_format = self._get_arg(self.data_formats.arg_name)
            row = [ f'\'{self.data_formats.arg_name}\'', data_format ]
            table.append(row)

        rows, _ = tabulate(table, '  ', left_align=[False, True])
        main = ['Received unavailable configuration:']
        main.extend(rows)
        main.append('')
        inst = (f'Use opcheck.list_configs(\'{self.op_path}\') to see all '
                f'available configurations')
        main.append(inst)
        msg = '\n'.join(main)
        return msg

    def _validate_schema(self, out_dir, test_ids=None):
        """
        Uses the gen_graph to produce a list of (<target_status>, <arg_dict>)
        pairs for the op.  <target_status> is the correct type of SchemaStatus.
        Runs the wrapped op on the <arg_dict> and collects the actual
        SchemaStatus and any possible exception from the framework.

        If {test_ids} is provided, it is an iterable of integers which specify
        a subset of tests to run.  This is useful for speeding up debugging.
        """
        if not os.path.exists(out_dir):
            raise RuntimeError(
                f'{type(self).__qualname__}: Could not open output path '
                f'\'{out_dir}\' for report generation')

        # list of successful arg_dict settings
        key_order = [ 'TP', 'TN', 'FP', 'FN', 'FAIL' ]
        suffixes = ['stats'] + [k for k in key_order]
        stats = { 'TP': [], 'FP': [], 'TN': [], 'FN': [], 'FAIL': [] }
        files = { sfx: os.path.join(out_dir,
            f'{self.op_path}.{sfx.lower()}.txt') for sfx in suffixes }

        print('Generating tests')
        config_list = list(fgraph.gen_graph_iterate(self.gen_graph.values()))
        # tests = [ _TestResult(self, 0, c) for c in cfgs ]
        # tests = [ t for t in tests if t.expect_class is not None ]
        # print(f'{len(tests)} tests')

        test_id = 1
        with open(files['TP'], 'w') as tp, \
                open(files['TN'], 'w') as tn, \
                open(files['FP'], 'w') as fp, \
                open(files['FN'], 'w') as fn, \
                open(files['FAIL'], 'w') as fail, \
                open(files['stats'], 'w') as stats_fh:
            fh = { 'TP': tp, 'TN': tn, 'FP': fp, 'FN': fn, 'FAIL':
                    fail, 'stats': stats_fh }
            is_first_line = True
            for config in config_list: 
                t = _TestResult(self, test_id, config)
                test_id += 1
                if t.expect_class is None:
                    test_id -= 1
                    continue
                if test_ids is not None:
                    if len(test_ids) == 0:
                        break
                    if t.id not in test_ids:
                        mat = ', '.join(f'{c}: {len(stats[c])}' for c in
                                key_order)
                        print('\r', end='')
                        print(f'Skipping test: {t.id:-4d}  Stats: {mat}', end='')
                        continue
                    test_ids.remove(t.id)

                t.run()
                cat = t.category
                row = t.stats()
                stats[cat].append(t)
                mat = ', '.join(f'{c}: {len(stats[c])}' for c in key_order)

                print('\r', end='')
                print(f'Running test: {t.id:-4d}  Stats: {mat}', end='')

                if is_first_line:
                    hdr = t.stat_keys()
                    hdr_line = '\t'.join(h for h in hdr)
                    print(hdr_line, file=fh['stats'])
                    is_first_line = False

                line = '\t'.join(r for r in row)
                print(line, file=fh['stats'])
                print(line, file=fh[cat])
                print(t.report(), file=fh[cat])

        print()
        print('Summary')
        for cat in key_order:
            res = stats[cat]
            print(f'{cat}: {len(res)}')

    def _passed(self):
        return (
                isinstance(self.input_status, Success) and
                isinstance(self.framework_status, Success)
                )

    def _shape_header(self, shape_arg):
        # translate a plain argument name  
        try:
            name, idx = shape_arg.split('.')
        except:
            name, idx = shape_arg, None

        if name not in self.params:
            raise RuntimeError(
                f'{type(self).__qualname__}: name \'{name}\' not a named '
                f'parameter.')
        cls = type(self.params[name].func)
        if cls == ge.TensorStub:
            sfx = 'shape'
        elif cls == ge.ShapeTensor:
            sfx = 'numpy()'
        elif cls == ge.ShapeTensor2D:
            if idx is not None:
                name = f'{name}[{idx},:]'
            sfx = 'numpy()'
        else:
            sfx = ''
        return name if sfx == '' else f'{name}.{sfx}'

    def _call_string(self, arg_dict):
        """
        Summarize the call arguments
        """
        reps = {}
        for name, arg_val in arg_dict.items():
            hdr = self._shape_header(name)
            cls = type(self.params[name].func)
            if cls == ge.TensorShape:
                val = arg_val.shape.as_list()
            elif cls == ge.ShapeTensor:
                val = arg_val.numpy().tolist()
            elif cls == ge.ShapeTensor2D:
                val = arg_val.numpy().tolist()
            else:
                val = arg_val
            reps[name] = f'{hdr}={repr(val)}'
        call_string = ', '.join(reps[n] for n in self.params.keys() if
                n in reps) 
        return call_string

    def _report(self):
        msg = self.input_status.message(self)
        print(msg, file=sys.stderr)

    def _get_arg(self, arg_name):
        """Retrieve the value of {arg_name} argument at call-time."""
        if arg_name not in self.params:
            raise SchemaError(
                f'\'{arg_name}\' not a known parameter. '
                f'Known parameters are: {self.params.keys()}')
        return self.arguments[arg_name]

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
    
    def _init_pred_graph(self):
        P.set_registry(self.pred_graph)

        # NodeFuncs
        ranks_obj = pr.Ranks(self, self.rank_candidates, self.rank_cons)
        dtypes_obj = pr.DTypes(self.dtype_cons)
        data_format_obj = pr.DataFormat(self.data_formats)
        layout_obj = pr.Layout(self.data_formats)

        # graph nodes
        schema = P.add_node(pr.Schema(self))
        shapes = P.add_node(pr.ShapeMap())
        sigs = P.add_node(pr.SigMap())
        data_format = P.add_node(data_format_obj, schema)
        layout = P.add_node(layout_obj, data_format)
        ranks = P.add_node(ranks_obj, shapes, data_format)
        dtypes = P.add_node(dtypes_obj, ranks, layout)
        P.add_node(pr.IndexDimsUsage(), ranks, sigs, shapes)

    def _init_gen_graph(self):
        G.set_registry(self.gen_graph)
        target_tensor_size = 1e5

        # NodeFuncs
        rank_stat_shape_obj = ge.RankStatusArgShape(self.dims_graph,
                self.rank_candidates, self.gen_indices, target_tensor_size)
        dtypes_status_obj = ge.DTypesStatus(self.dtype_cons)
        data_format_obj = ge.DataFormat(self.data_formats)

        # graph nodes
        layout = G.add_node(ge.Layout(self))
        sigs = G.add_node(ge.SigMap())
        rank_stat_shape = G.add_node(rank_stat_shape_obj, sigs) 
        ranks = G.add_node(ge.GetRanks(), rank_stat_shape)
        status_arg = G.add_node(ge.GetStatusArgShape(), rank_stat_shape)
        data_format = G.add_node(data_format_obj, ranks, layout)
        dtypes_status = G.add_node(dtypes_status_obj, ranks, layout)
        G.add_node(ge.GetArgShapes(), status_arg)
        G.add_node(ge.GetDTypes(), dtypes_status)
        G.add_node(ge.StatusAggregator(), dtypes_status, status_arg) 

    def _add_inv_graph(self):
        """
        Graph for producing the inventory of valid call configurations for
        data format, tensor signatures and dtypes.  This graph consists of
        generative nodes:

        SigMap, Sig*, Layout, Ranks, ValidDTypes 
        """
        G.set_registry(self.inv_graph)
        to_clone = (ge.SigMap, ge.Layout, ge.Sig)
        for kn, nd in self.gen_graph.items():
            if isinstance(nd.func, to_clone):
                self.inv_graph[kn] = nd.clone_node_only()

        # now fix parent pointers
        for kn, nd in self.inv_graph.items():
            orig_node = self.gen_graph[kn]
            for orig_pa in orig_node.parents:
                new_pa = self.inv_graph[orig_pa.name]
                nd.append_parent(new_pa)
                
        ranks_obj = ge.Ranks(self, self.rank_candidates)
        dtypes_obj = ge.ValidDTypes(self.dtype_cons)
        data_format_obj = ge.DataFormat(self.data_formats)
        ranks = G.add_node(ranks_obj)
        layout = self._inv_node(ge.Layout)
        G.add_node(dtypes_obj, ranks, layout)
        G.add_node(data_format_obj, ranks, layout)

    def _add_pred_graph(self):
        # add single-index dims nodes that are not already added
        P.set_registry(self.pred_graph)
        idims_usage = self._pred_node(pr.IndexDimsUsage)

        for node_name, parent_idxs in self.pending_index_edges.items():
            node = self.pred_graph[node_name]
            for idx in parent_idxs:
                idx_node = self._pred_node(pr.ComputedDims, idx)
                if idx_node is not None:
                    node.append_parent(idx_node)
                    continue
                idx_node = self._pred_node(pr.SingleIndexDims, idx)
                if idx_node is None:
                    si_obj = pr.SingleIndexDims(idx)
                    idx_node = P.add_node(si_obj, idims_usage)
                node.append_parent(idx_node)

        for node_name, parent_names in self.pending_pred_edges.items():
            node = self.pred_graph[node_name]
            for parent_name in parent_names:
                parent_node = self.pred_graph[parent_name]
                node.append_parent(parent_node)

        # move pr.GetReturnTensor* and pr.ValidReturnShape* nodes 
        for i in range(self.num_returns):
            ret_tensor = fgraph.node_name(pr.GetReturnTensor, str(i))
            valid_return = fgraph.node_name(pr.ValidReturnShape, str(i))
            ret_node = self.pred_graph.pop(ret_tensor)
            self.return_pred_graph[ret_tensor] = ret_node
            valid_ret_node = self.pred_graph.pop(valid_return)
            self.return_pred_graph[valid_return] = valid_ret_node

    def _finalize(self):
        self.dims_graph.finalize()

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
            self.rank_candidates.add_rank_limits(idx, min_rank, max_rank)

    def arg_unchecked(self, arg_name):
        """
        Declare {arg_name} to be an argument unchecked by OpCheck 
        """
        self.params[arg_name] = None

    def computed_index(self, comp_index, comp_func, tem_func, input_indexes,
            min_val, *extra_args):
        """
        Registers {comp_func} to compute the dimensions of {comp_index}.
        Registers {tem_func} which produces a text string explaining how
        the index is computed.

        Adds an index predicate to ensure all components of the computed index
        are >= {min_val}

        {extra_args} must be names of the framework op's parameters 

        The following calls are made:

        comp_func(*index_dims, *extra_vals)
        (index_dims are the resolved dimensions of {input_indexes})
        (extra_vals are the runtime values of {extra_args})

        If a downstream constraint (registered with add_index_predicate) fails,
        then OpCheck makes these calls:

        tem_func(*index_desc, *extra_vals)
        tem_func(*index_dims, *extra_vals)
        (index_desc are the snake_cased descriptions of {input_indexes})

        for any computed indices that are used directly or indirectly by the
        predicate (ancestor indices).  The output strings are then assembled to
        create an explanatory error message.
        """
        if not all(idx in self.index for idx in input_indexes):
            raise SchemaError(
                f'{type(self).__qualname__}: In schema \'{self.op_path}\'.\n'
                f'Indices string \'{input_indexes}\' contains unregistered '
                f'indices.\nRegistered indices are: {list(self.index.keys())}\n'
                )

        extra_nodes = [ self.params[n] for n in extra_args ]
        extra_node_names = [ e.name for e in extra_nodes ]
        nidx = len(input_indexes)
        comp_obj = pr.ComputedDims(comp_index, comp_func, nidx)
        comp_dims = P.add_node(comp_obj)
        tem_obj = pr.TemplateFunc(comp_index, tem_func, nidx, self)
        tem = P.add_node(tem_obj, comp_dims)
        self.comp_dims_templates[comp_index] = tem

        index_node_names = []
        for idx in input_indexes:
            name = fgraph.node_name(pr.SingleIndexDims, idx)
            index_node_names.append(name)
            
        self.pending_index_edges[tem.name] = input_indexes
        self.pending_index_edges[comp_dims.name] = input_indexes
        self.pending_pred_edges[tem.name] = extra_node_names 
        self.pending_pred_edges[comp_dims.name] =  extra_node_names

        if comp_index in self.dims_graph.computed_indexes():
            raise SchemaError(
                f'{type(self).__qualname__}: index \'{comp_index}\' has '
                f'already been registered as a computed index')
        if comp_index in self.dims_graph.input_indexes():
            raise SchemaError(
                f'{type(self).__qualname__}: index \'{comp_index}\' has '
                f'already been used as an input index for some computed '
                f'index.  Calls to computed_index must be in dependency order')

        self.dims_graph.add_comp_index(comp_index, comp_func, input_indexes,
                *extra_args)

        dims_node = self.gen_graph[fgraph.node_name(ge.RankStatusArgShape)]
        for nd in extra_nodes:
            dims_node.maybe_append_parent(nd)

        # add a predicate to ensure the computed index is >= some minimum value
        bounds_pobj = flib.PredAbove(min_val)
        self.add_index_predicate(f'{comp_index} >= {min_val}', bounds_pobj,
                comp_index)  

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
        if (self.rank_candidates.index_limited(target_index) or
                self.rank_candidates.index_equated(target_index)):
            raise SchemaError(
                f'{type(self).__qualname__}: target index \'{target_index}\''
                f'is already registered as constrained')
        if not self.rank_candidates.index_limited(source_index):
            raise SchemaError(
                f'{type(self).__qualname__}: source index \'{source_index}\''
                f'is not constrained with limit_ranks')
        self.rank_candidates.equate_ranks(target_index, source_index)

    def limit_ranks(self, sig, min_val, max_val):
        """
        Declare that the rank of {sig} be in [{min_val}, {max_val}]
        """
        self._check_sig(sig, 'rank limits')
        self.rank_candidates.add_rank_limits(sig, min_val, max_val)

    @staticmethod
    def _dtype_expr(type_expr):
        exprs = {
                'int': [8, 16, 32, 64],
                'uint': [8, 16, 32, 64],
                'float': [16, 32, 64],
                'qint': [8, 16, 32],
                'bfloat': [16],
                'bool': [''],
                'complex': [64, 128]
                }

        types = [ ', '.join(f'{k}{v}' for v in exprs[k]) for k in exprs ]
        type_str = '\n'.join(t for t in types)
        err_msg = SchemaError(
            f'Received invalid dtype expression \'{type_expr}\'.\n'
            f'dtype expression must match the pattern:\n'
            f'([a-z]+)(8|16|32|64|128)?([\+\-])?\n'
            f'The first capture is the data type and must be one of: '
            f'int, uint, float, qint, bfloat, bool, complex\n'
            f'The second capture is the size.  It is optional. '
            f'The third is an optional \'+\' or \'-\''
            f'The list of valid constructed types are:\n'
            f'{type_str}\n'
            )

        # expect format to be {pfx}{q}[+-]*
        ma = re.match('([a-z]+)(8|16|32|64|128)?([\+\-])?', type_expr)
        if ma is None:
            raise err
        pfx, q, rng = ma.groups()
        if q is None:
            ids = [ f'{pfx}{sz}' for sz in exprs[pfx] ]
        else:
            if rng is None:
                ids = [ type_expr ]
            elif rng == '+':
                ids = [ f'{pfx}{sz}' for sz in exprs[pfx] if sz >= int(q) ]
            else:
                ids = [ f'{pfx}{sz}' for sz in exprs[pfx] if sz <= int(q) ]
        try:
            dtypes = [ tf.dtypes.as_dtype(i) for i in ids ]
        except TypeError:
            raise err
        return dtypes

    def _is_data_tensor(self, name):
        return (name in self.params and isinstance(self.params[name].func, 
                ge.TensorStub))

    def valid_dtypes(self, tensor_name, type_list):
        """
        Declare that {tensor_name} can have any of the dtype strings in
        {type_list}.  Names in {type_list} fit the pattern:

        ([a-z]+)(8|16|32|64|128)?([\+\-])?
        The first capture is the data type and must be one of:
        int, uint, float, qint, bfloat, bool, complex
        The second capture is the size.  It is optional.
        The third is an optional '+' or '-'.
        If the second is not present, the third must not be present.

        A prefix alone denotes all sizes of that data type are valid.
        A prefix with a quantity and no '+' or '-' specifies that single dtype.
        If a '+' is included, it means, that size and larger.
        If a '-' is included, it means that size and smaller.

        Can only be called once for a given {tensor_name}
        """
        if not self._is_data_tensor(tensor_name):
            raise SchemaError(
                f'{type(self).__qualname__}: Parameter \'{tensor_name}\' is '
                f'not registered as a tensor')
        if self.dtype_cons.has_valid_dtypes(tensor_name):
            raise SchemaError(
                f'{self.__qualname__}: Tensor \'{tensor_name}\' is already '
                f'registered with valid dtypes')

        dtypes = [ t for ex in type_list for t in self._dtype_expr(ex) ]
        self.dtype_cons.add_valid(tensor_name, dtypes)

    def equate_dtypes(self, trg_tensor, src_tensor):
        """
        Declare that {trg_tensor} have the same dtype as {src_tensor}.
        Both must be tensors declared with arg_tensor.
        Can only be called once for a given {trg_tensor}
        """
        if not (self._is_data_tensor(src_tensor) and
                self._is_data_tensor(trg_tensor)):
            raise SchemaError(
                f'{type(self).__name__}: Can only be called on two tensors. '
                f'Parameters \'{src_tensor}\' and \'{trg_tensor}\' are not '
                f'both tensors.')
        prev_equate_src = self.dtype_cons.get_equate_source(trg_tensor)
        if prev_equate_src is not None:
            raise SchemaError(
                f'{type(self).__name__}: Tensor \'{trg_tensor}\' has already '
                f'been assigned dtype equated source tensor '
                f'\'{prev_equate_src}\' from a previous call to equate_dtypes')
        self.dtype_cons.add_equiv(trg_tensor, src_tensor)

    def exclude_dtypes(self, fields, *exclude):
        """
        This API call allows to mark any combinations of tensor dtypes, index
        ranks and layouts as excluded.  It is useful in cases where such
        combinations are not implemented by the framework.

        Register {exclude} combinations of tensor dtypes, index ranks and
        layout to be excluded.

        {fields} is a comma-separated list of fields, with any of:
        - data tensor names registered with arg_tensor
        - one-letter index names registered with add_index
        - the constant ':layout'

        Each member of {exclude} contains a tuple corresponding to {fields}.
        - data tensor fields have a dtype string, such as 'int32'
        - one-letter indexes have an integer specifying a rank of that index
        - the Kind.LAYOUT field has an integer in [0, num_layouts), as defined
          by the call to arg_layout.

        A value of None for any field indicates a wild-card, meaning 'exclude
        all values'.

        In the rare case a tensor is a one-letter name and conflicts with an
        index name, the first occurrence is interpreted as a tensor name, and
        the second as an index name.
        """
        tensors = []
        indexes = []
        has_layout = False
        
        for f in fields:
            if self._is_data_tensor(f) and f not in tensors:
                tensors.append(f)
            elif f in self.index:
                indexes.append(f)
            elif f == ':layout':
                has_layout = True
            else:
                raise SchemaError(
                    f'{type(self).__qualname__}: Item \'{f}\' in fields was '
                    f'not a data tensor registered with arg_tensor or '
                    f'one letter index name registered with add_index, or '
                    f'the constant \':layout\'')

        num_fields = len(fields)
        num_tensors = len(tensors)
        num_indexes = len(indexes)
        for ex in exclude:
            if len(ex) != num_fields:
                raise SchemaError(
                    f'{type(self).__qualname__}: Each item in \'exclude\' '
                    f'must have the same number of elements as \'fields\'.\n'
                    f'Found {len(fields)} fields but exclude item '
                    f'{ex} has {len(ex)} fields.')
            it = iter(ex)
            dtype_bases = []
            ranks = {} 
            for i in range(num_tensors):
                dtype_expr = next(it)
                dtype_list = self._dtype_expr(dtype_expr)
                dtype_bases.append(dtype_list)
            for p, idx in enumerate(indexes):
                rank = next(it)
                if rank is None:
                    continue
                elif isinstance(rank, int):
                    ranks[idx] = rank
                else:
                    raise SchemaError(
                        f'{type(self).__qualname__}: Got invalid rank item in '
                        f'exclusion tuple \'{ex}\'. Item {num_tensors + p - 1}'
                        f' was \'{rank}\' but should be None or an integer.')
            if has_layout:
                layout = next(it)
                if layout is None:
                    pass
                elif (isinstance(layout, int) and layout in
                        range(self.data_formats.num_layouts())):
                    pass
                else:
                    raise SchemaError(
                        f'{type(self).__qualname__}: Got invalid layout '
                        f'\'{layout}\'.  Must be None or an integer in '
                        f'[0, {self.num_layouts})')
            for dtypes in itertools.product(*dtype_bases):
                self.dtype_cons.add_excluded(tensors, dtypes, ranks, layout)

    def arg_int(self, arg_name, lo=None, hi=None):
        """
        Declare {arg_name} to be an integer that can take on values in a range.
        If {lo} is None, it is sys.maxint
        If {hi} is None, it is -sys.maxint-1 
        """
        pred_obj = pr.ArgInt(arg_name, lo, hi)
        gen_obj = ge.Int(lo, hi)
        schema = self._pred_node(pr.Schema)
        P.add_node(pred_obj, schema)
        arg = G.add_node(gen_obj)
        self.params[arg_name] = arg

    def arg_option(self, arg_name, options):
        """
        Expect {arg_name} to take on one of the values in {options}
        """
        options_gobj = ge.Options(arg_name, options)
        arg = G.add_node(options_gobj)
        options_pobj = pr.Options(arg_name, options)
        schema = self._pred_node(pr.Schema)
        P.add_node(options_pobj, schema)
        self.params[arg_name] = arg

    def arg_layout(self, arg_name, layouts, rank_idx):
        """
        Declares {arg_name} to control layout-dependent signatures for tensors. 
        {layouts} is an array, where each element is a map of: rank => code
        The rank of {rank_idx} determines which layout is mapped.
        """
        G.set_registry(self.gen_graph)
        self.data_formats.configure(arg_name, layouts, rank_idx)
        
        # define the real arg 
        data_format = self._gen_node(ge.DataFormat)
        self.params[arg_name] = data_format

        # edge: ge.DTypesStatus -> ge.Layout
        layout = self._gen_node(ge.Layout)
        dtypes_status = self._gen_node(ge.DTypesStatus)
        dtypes_status.append_parent(layout)

    def _check_sigs_layout(self, arg_name, sigs_list):
        num_layouts = self.data_formats.num_layouts()
        if len(sigs_list) == 1:
            sigs_list = sigs_list * num_layouts

        if len(sigs_list) != num_layouts:
            raise SchemaError(
                f'{type(self).__qualname__}: registering \'{arg_name}\' '
                f'there are {self.num_layouts} '
                f'layouts (as established by the call to \'arg_layout\') but '
                f'{len(sigs_list)} elements of \'sigs\' argument.')
        return sigs_list 

    def _arg_shape_func(self, arg_name, sigs_list, pred_node, pred_obj, gen_obj):
        """
        Backend function for arg_shape_* API functions.
        sigs_list must be a list of either 1 or num_layout elements.  If 1, it
        is implicitly broadcasted to num_layouts
        """
        sigs_list = self._check_sigs_layout(arg_name, sigs_list)
        # node: ge.Sig 
        # node: one of ge.TensorStub, ge.ShapeList, ge.ShapeInt, ge.ShapeTensor    
        # edges: ge.SigMap -> ge.Sig, [newnode] -> ge.RankStatusArgShape 
        sig_obj = ge.Sig(arg_name, sigs_list)
        layout = self._gen_node(ge.Layout)
        shape = self._gen_node(ge.GetArgShapes)
        sig_map = self._gen_node(ge.SigMap)
        sig = G.add_node(sig_obj, layout)
        if isinstance(gen_obj, ge.TensorStub):
            dtypes = self._gen_node(ge.GetDTypes)
            arg_node = G.add_node(gen_obj, shape, dtypes)
        else:
            arg_node = G.add_node(gen_obj, shape)
        self.params[arg_name] = arg_node
        sig_map.append_parent(sig)

        # node: pr.Sig
        # node: one of pr.TensorStub, pr.ShapeList, pr.ShapeInt, pr.ShapeTensor  
        # edges:
        # pr.Shape -> pred_node
        # pr.ShapeMap -> pr.Shape
        layout = self._pred_node(pr.Layout)
        sig_obj = pr.Sig(arg_name, sigs_list)
        sig = P.add_node(sig_obj, layout)
        sig_map = self._pred_node(pr.SigMap)
        sig_map.append_parent(sig)
        shape_pobj = pred_obj
        shape = P.add_node(shape_pobj, pred_node)
        shape_map = self._pred_node(pr.ShapeMap)
        shape_map.append_parent(shape)
        cons = base.ShapeRankConstraint(arg_name, pred_obj.__class__)
        self.rank_cons.append(cons)

    def arg_tensor(self, arg_name, *sigs):
        """
        Register {arg_name} as a tensor.  

        sigs are all strings of signatures.  If len(sigs) == 1, then it
        specifies a static signature regardless of whether 'arg_layout' was
        called.  If len(sigs) > 1, then arg_layout is required to be called
        before this call.
        """
        schema = self._pred_node(pr.Schema)
        arg_pobj = pr.DataTensor(arg_name)
        pred_arg = P.add_node(arg_pobj, schema)
        pred_obj = pr.TensorShape(arg_name)
        gen_obj = ge.TensorStub(arg_name)
        self._arg_shape_func(arg_name, sigs, pred_arg, pred_obj, gen_obj)

        gnode = self.gen_graph[gen_obj.name]
        dtypes = self._gen_node(ge.GetDTypes)
        gnode.maybe_append_parent(dtypes)

        # nodes: pr.TensorDType
        # pr.TensorDType -> pr.Arg
        # pr.DTypes -> pr.TensorDType
        dtypes = self._pred_node(pr.DTypes)
        tensor_dtype_obj = pr.TensorDType(arg_name)
        dtype = P.add_node(tensor_dtype_obj, pred_arg)
        dtypes.append_parent(dtype)

    def arg_shape_list(self, arg_name, *sigs):
        """
        Register {arg_name} as an integer list parameter which defines the
        shape of a signature.  
        """
        # Creates nodes:
        # arg_name:arg (int list)
        # arg_name:shape (int list, the same value as :arg)
        # arg_name:sig (str, the associated signature)
        schema = self._pred_node(pr.Schema)
        arg_pobj = pr.ArgType(arg_name, list)
        pred_arg = P.add_node(arg_pobj, schema)
        pred_obj = pr.ShapeList(arg_name)
        gen_obj = ge.ShapeList(arg_name)
        self._arg_shape_func(arg_name, sigs, pred_arg, pred_obj, gen_obj)

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
        schema = self._pred_node(pr.Schema)
        arg_pobj = pr.ArgType(arg_name, int)
        pred_arg = P.add_node(arg_pobj, schema)

        pred_obj = pr.ShapeInt(arg_name)
        gen_obj = ge.ShapeInt(arg_name)
        self._arg_shape_func(arg_name, (index,), pred_arg, pred_obj, gen_obj) 

    def arg_shape_tensor(self, arg_name, *sigs):
        """
        Register {arg_name} as a 1D integer tensor whose elements define the
        shape of a signature.  
        """
        schema = self._pred_node(pr.Schema)
        arg_pobj = pr.ArgType(arg_name, int)
        pred_arg = P.add_node(arg_pobj, schema)

        pred_obj = pr.ShapeTensor(arg_name)
        gen_obj = ge.ShapeTensor(arg_name)
        self._arg_shape_func(arg_name, sigs, pred_arg, pred_obj, gen_obj)

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
        # created nodes
        # pr.ShapeTensor2D, ge.ShapeTensor2D
        # ge.Sig(i), pr.Sig(i)
        # pr.SliceShape(i)

        # created edges
        # pr.ShapeTensor2D -> pr.Schema
        # ge.ShapeTensor2D -> ge.GetArgShapes
        # pr.SliceShape(i) -> pr.ShapeTensor2D
        # pr.Sig(i) -> pr.Layout, ge.Sig(i) -> ge.Layout
        # pr.Sigmap -> pr.Sig(i), ge.SigMap -> ge.Sig(i)
        schema = self._pred_node(pr.Schema)
        shape2d_gobj = ge.ShapeTensor2D(arg_name, len(sigs))
        shape2d_pobj = pr.ShapeTensor2D(arg_name, len(sigs))
        arg_tensor = P.add_node(shape2d_pobj, schema)
        self.params[arg_name] = arg_tensor

        arg_shapes = self._gen_node(ge.GetArgShapes)
        shape2d = G.add_node(shape2d_gobj, arg_shapes)

        g_sig_map = self._gen_node(ge.SigMap)
        g_layout = self._gen_node(ge.Layout)
        p_sig_map = self._pred_node(pr.SigMap)
        p_shape_map = self._pred_node(pr.ShapeMap)
        p_layout = self._pred_node(pr.Layout)

        for i, sig in enumerate(sigs):
            prefix = f'{arg_name}.{i}'

            # pr.ShapeMap -> pr.SliceShape
            shp_pobj = pr.SliceShape(arg_name, i)
            p_shp = P.add_node(shp_pobj, arg_tensor)
            p_shape_map.append_parent(p_shp)

            cons = base.SliceRankConstraint(arg_name, i)
            self.rank_cons.append(cons)

            if isinstance(sig, str):
                sig = [sig]
            g_sig_obj = ge.Sig(prefix, sig)
            p_sig_obj = pr.Sig(prefix, sig)
            g_sig = G.add_node(g_sig_obj, g_layout)
            p_sig = P.add_node(p_sig_obj, p_layout)
            g_sig_map.append_parent(g_sig)
            p_sig_map.append_parent(p_sig)

    def arg_rank(self, arg_name, sig):
        """
        Register {arg_name} to be an integer argument which defines the rank of
        {sig}
        """
        # arg_kname = base.kname(arg_name, Kind.ARG)
        cons_name = f'rank({sig}) == \'{arg_name}\''
        rank_pobj = pr.ArgInt(arg_name, 0, None)

        cons = base.IntRankConstraint(cons_name, rank_pobj.name, sig)
        self.rank_cons.append(cons)
        schema = self._pred_node(pr.Schema)
        p_rank = P.add_node(rank_pobj, schema)
        self.params[arg_name] = p_rank 

        g_ranks = self._gen_node(ge.GetRanks)
        G.add_node(ge.Rank(sig), g_ranks)

        p_ranks = self._pred_node(pr.Ranks)
        p_ranks.maybe_append_parent(p_rank)

    def rank_dims_constraint(self, constraint_name, get_dims, rank_sig,
            dims_index, shape_arg):
        """
        Creates a constraint called {constraint_name} with the logic:
        RANK(rank_sig) == get_dims(shape_arg).

        Creates a generated index dimension:
        DIMS(dims_index) <- RANK(rank_sig)

        get_dims(*args) must return the quantity equal to DIMS(dims_index).
        Since this quantity is not directly available during the rank inference
        phase, it must use other means to derive the quantity.
        """
        cons = base.DimRankConstraint(constraint_name, rank_sig, shape_arg,
                get_dims, dims_index)
        self.rank_cons.append(cons)
        shape = self.params[shape_arg]

        rank = self._pred_node(pr.Ranks)
        rank.maybe_append_parent(shape)

        # 'sum' simply sums up the individual ranks of indices in rank_sig 
        def gen_single_index(ranks_list):
            val = sum(ranks_list)
            return [([val],)]

        dims_gobj = ge.Dims(gen_single_index, dims_index, rank_sig, tuple())

        g_ranks = self._gen_node(ge.GetRanks)
        arg_shapes_ranks = self._gen_node(ge.RankStatusArgShape)
        dims = G.add_node(dims_gobj, g_ranks)
        arg_shapes_ranks.append_parent(dims)
        self.dims_graph.maybe_add_input_index(dims_index)

    def add_index_predicate(self, pred_name, status_func, indices):
        """
        Registers {status_func} with the schema to be used as an additional
        predicate for {indexes} dimensions.

        {pred_name} is a name given to this custom predicate.  It may be used
        in error messages.

        Called as status_func(*index_shapes), where index_shapes are the
        resolved shapes of each index, in order, in {indices}.  They are
        provided as numpy arrays.

        status_func must return an instance of SchemaStatus

        Custom status functions are found in the flib module.
        """
        id_cons_obj = pr.IndexDimsConstraint(pred_name, status_func)
        # dims_kname = kname(pred_name, Kind.NONE)
        ranks = self._pred_node(pr.Ranks)
        sig_map = self._pred_node(pr.SigMap)
        shape_map = self._pred_node(pr.ShapeMap)
        schema = self._pred_node(pr.Schema)
        id_cons = P.add_node(id_cons_obj, ranks, sig_map, shape_map, schema)
        self.pending_index_edges[id_cons.name] = indices

    def add_index_generator(self, output_indices, gen_func, input_indices, 
            *gen_args):
        """
        Registers {gen_func} with the schema to be used to generate
        dimension combinations for {output_indices}, which is a string
        consisting of individual index one-letter codes.

        It is called as gen_func(input_ranks, *gen_args) and returns a list of
        shape tuples.  The shapes in each shape tuple correspond with the
        indices in output_indices.

        input_ranks are the resolved ranks of input_indices

        Custom generator function objects are found in the flib module.
        """
        self.gen_indices.add_generator(gen_func, output_indices, input_indices,
                gen_args)

    # TODO: should I clone the graph, or simply set the parents to nodes in the
    # gen graph?
    def return_tensor(self, *sigs):
        """
        Append a return tensor to the list of expected return tensors.

        *sigs may contain either one element, or {num_layout} elements.  If one
        element, it defines the static signature for the return tensor.  If
        multiple, they are defined by the provided layout as declared in
        'arg_layout'
        """
        index = self.num_returns
        sigs_list = self._check_sigs_layout(f'return[{index}]', sigs)

        prefix = f'return[{index}]'
        g_sig_obj = ge.Sig(prefix, sigs_list)
        p_sig_obj = pr.Sig(prefix, sigs_list)

        rten_pobj = pr.GetReturnTensor(index)
        rvalid_pobj = pr.ValidReturnShape(index)
        pred_shape_pobj = pr.PredictedShape(index)

        schema = self._pred_node(pr.Schema)
        layout = self._pred_node(pr.Layout)
        rten = P.add_node(rten_pobj, schema)
        sig = P.add_node(p_sig_obj, layout)
        pred_shape = P.add_node(pred_shape_pobj, sig)

        sig_inds = { idx for sig in sigs for idx in sig }
        self.pending_index_edges[pred_shape.name] = ''.join(sig_inds)
        P.add_node(rvalid_pobj, rten, pred_shape)

        layout = self._gen_node(ge.Layout)
        sig = G.add_node(g_sig_obj, layout)
        sig_map = self._gen_node(ge.SigMap)
        sig_map.append_parent(sig)
        # sig_gnode = G.add_node(sig_kname, sig_gobj, *layout) 
        # sig_map_gnode = G.get_node(Kind.SIG_MAP)
        # sig_map_gnode.append_parent(sig_gnode)
        self.num_returns += 1

