from opschema import genlib
from opschema.complib import dilate, dilate_t, tconv, tconv_t

def init_schema(op):
    op.add_index('b', 'batch', (1, 10))
    op.add_index('i', 'input spatial', (1,3))
    op.add_index('k', 'input channel', 1)
    op.add_index('j', 'strided input spatial', 'i')
    op.add_index('f', 'filter spatial', 'i')
    op.add_index('g', 'dilated filter spatial', 'i')
    op.add_index('s', 'strides', 'i')
    op.add_index('d', 'dilations', 'i')
    op.add_index('l', 'output channel', 1)
    op.add_index('o', 'output spatial', 'i')

    op.arg_tensor('input', 'bik')
    op.arg_tensor('filters', 'flk')
    op.arg_shape_tensor('output_shape', 'bol')
    op.arg_shape_bcast_list('strides', 's')
    op.arg_shape_bcast_list('dilations', 'd')
    op.arg_option('padding', ('VALID', 'SAME'))
    op.arg_unchecked('name')
    op.return_tensor('bol')

    op.gen_dims('b', 100)
    op.gen_dims('f', 100)
    op.gen_dims_func('i', genlib.below_above, 'f', 1000, False)  
    op.gen_dims_func('s', genlib.stride_dil, '', 10, True) 
    op.gen_dims_func('d', genlib.stride_dil, '', 10, True) 
    op.gen_dims('k', 30)
    op.gen_dims('l', 30)

    jdims =   lambda i, s: i * s
    jdims_t = lambda i, s: f'{i} * {s}'

    # input is dilated with 'strides' 
    op.comp_dims_cw('j', dilate, dilate_t, 'is') 
    # filter is dilated with 'dilations'
    op.comp_dims_cw('g', dilate, dilate_t, 'fd')

    def odims_gen(j, g):
        val = j + g - 1
        yield val, val

    op.gen_dims_func('o', odims_gen, 'jg', 1000, False)   

    op.valid_dtypes('input', ('float',))
    op.equate_dtypes('filters', 'input')

