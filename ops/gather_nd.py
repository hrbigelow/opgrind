import opcheck
from schema import Kind, kname 

def init_schema(op):
    op.add_index('b', 'batch')
    op.add_index('r', 'read location', 1, None)
    op.add_index('w', 'write location', 1, None)
    op.add_index('e', 'slice element')
    op.add_index('c', 'read address component', 1, 1)

    # def genc(rank_list):
        # return [([rank_list[0]],)]
    # op.add_index_generator(genc, 'c', 'r')

    # allowed rank combinations
    op.limit_ranks('bre', None, 8)
    op.limit_ranks('bwc', None, 8)

    # argument interpretations
    op.arg_tensor('indices', 'bwc')
    op.arg_tensor('params', 'bre')
    op.arg_rank('batch_dims', 'b')
    op.arg_unchecked('name')

    # dtypes
    op.valid_dtypes('indices', ('int32', 'int64'))
    op.valid_dtypes('params', ('int32', 'float32'))

    def rankr(indices_shape):
        return indices_shape[-1]
    op.rank_dims_constraint('rank(r) == dims(c)', rankr, 'r', 'c', 'indices')

    # output shape prediction
    op.return_tensor('bwe')

    
opcheck.register('tf.gather_nd', init_schema)

"""
Rank Inference is unambiguous:
rank(c) = 1
rank(b) = batch_dims
rank(w) = rank(indices) - rank(c) - rank(b)
rank(r) = dims(c)[0]

rank inference constraints - necessary to infer the actual rank combos from a
given call

from TensorFlow docs
(https://www.tensorflow.org/api_docs/python/tf/gather_nd)
index_depth = indices.shape[-1]
outer_shape = indices.shape[:-1]
assert index_depth <= params.shape.rank
inner_shape = params.shape[index_depth:]
output_shape = outer_shape + inner_shape

Interpretation:
inner_shape = e (slice element)  
outer_shape = bw (batch + write location) 
output_shape = bwe (outer_shape + inner_shape)
"""

