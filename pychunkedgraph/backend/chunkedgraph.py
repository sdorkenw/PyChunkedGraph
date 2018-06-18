from bitstring import BitArray
import collections
import numpy as np
import time
import datetime
import os
import networkx as nx
from networkx.algorithms.flow import shortest_augmenting_path
import pytz

from . import multiprocessing_utils as mu
from google.cloud import bigtable
from google.api_core.retry import Retry, if_exception_type
from google.api_core.exceptions import Aborted, DeadlineExceeded, \
    ServiceUnavailable

# global variables
HOME = os.path.expanduser("~")
N_DIGITS_UINT64 = len(str(np.iinfo(np.uint64).max))
LOCK_EXPIRED_TIME_DELTA = datetime.timedelta(minutes=5)
UTC = pytz.UTC

# Setting environment wide credential path
os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = \
    HOME + "/.cloudvolume/secrets/google-secret.json"


def log_n(arr, n):
    if n == 2:
        return np.log2(arr)
    elif n == 10:
        return np.log10(arr)
    else:
        return np.log(arr) / np.log(n)


def to_bitstring(node_id, bit_width=None, is_bytes=False, endian="little"):
    if not is_bytes:
        node_id_b = int(node_id).to_bytes((int(node_id).bit_length() + 7) // 8,
                                          byteorder=endian, signed=False)
    else:
        node_id_b = int(node_id)

    node_id_b = BitArray(node_id_b).bin

    if bit_width is not None:
        if bit_width > len(node_id_b):
            node_id_b = "".join(["0" * (bit_width - len(node_id_b)),
                                 node_id_b])
        elif bit_width < len(node_id_b):
            node_id_b = node_id_b[len(node_id_b) - bit_width:]

    return node_id_b


def to_int(node_id_bits, endian):
    node_id_size = len(node_id_bits)

    assert node_id_size <= 64

    node_id_bits = "".join(['0' * (64 - node_id_size), node_id_bits])

    return int.from_bytes(BitArray("".join(['0b', node_id_bits])).tobytes(), byteorder=endian)


def bitwise_inc(node_id_b, from_start=True):
    assert "0" in node_id_b

    inc_node_id_b = list(node_id_b)

    if not from_start:
        inc_node_id_b = inc_node_id_b[::-1]

    loc = 0
    while(inc_node_id_b[loc]) == "1":
        inc_node_id_b[loc] = "0"
        loc += 1

    inc_node_id_b[loc] = "1"

    if not from_start:
        inc_node_id_b = inc_node_id_b[::-1]

    return "".join(inc_node_id_b)


def get_clean_cg_object(table_id, n_layers, fan_out,
                        instance_id="pychunkedgraph",
                        project_id="neuromancer-seung-import",
                        chunk_size=(512, 512, 64)):
    """ Creates clean chunkedgraph object

    deletes old table and creates new one

    :param table_id: str
    :param instance_id: str
    :param project_id: str
    :param chunk_size: list of 3 ints
    :return: ChunkedGraph object
    """

    try:
        cg = ChunkedGraph(table_id=table_id, instance_id=instance_id,
                          project_id=project_id, chunk_size=chunk_size)
        cg.table.delete()
        print("Deleted existing table")
        time.sleep(1)
    except:
        pass

    cg = ChunkedGraph(table_id=table_id, instance_id=instance_id,
                      project_id=project_id, chunk_size=chunk_size,
                      n_layers=n_layers, fan_out=fan_out)

    return cg


def bitinv_int(node_id, check_bit_width=64):
    """ Inverts bit order

    :param node_id: int
    :param check_bit_width: int or None
        if not None: asserts bit width to given int
    :return: int
    """
    node_id_bitinv = to_bitstring(node_id, endian="little")[::-1]

    if check_bit_width is not None:
        assert len(node_id_bitinv) == check_bit_width

    return int(node_id_bitinv, 2)


def serialize_node_id(node_id):
    """ Serializes an id to be ingested by a bigtable table row

    :param node_id: int
    :return: str
    """
    return serialize_key(str(bitinv_int(node_id)))


def deserialize_node_id(node_id):
    """ De-serializes a node id from a BigTable row

    :param node_id: int
    :return: str
    """
    return int(bitinv_int(node_id.decode()))


def serialize_key(key):
    """ Serializes a key to be ingested by a bigtable table row

    :param key: str
    :return: str
    """
    return key.encode("utf-8")


def compute_bitmasks(n_layers, fan_out):
    """

    :param n_layers: int
    :return: dict
        layer -> bits for layer id
    """

    bitmask_dict = {}
    for i_layer in range(n_layers, 0, -1):

        if i_layer == 1:
            # Lock this layer to an 8 bit layout to maintain compatibility with
            # the exported segmentation

            # n_bits_for_layers = np.ceil(log_n(fan_out**(n_layers - 2), fan_out))
            n_bits_for_layers = 8
        else:
            n_bits_for_layers = max(1, np.ceil(log_n(fan_out**(n_layers - i_layer), fan_out)))
            # n_bits_for_layers = fan_out ** int(np.ceil(log_n(n_bits_for_layers, fan_out)))

        n_bits_for_layers = int(n_bits_for_layers)

        assert n_bits_for_layers <= 8

        bitmask_dict[i_layer] = n_bits_for_layers
    return bitmask_dict


def mincut(edges, affs, source, sink):
    """ Computes the min cut on a local graph

    :param edges: n x 2 array of uint64s
    :param affs: float array of length n
    :param source: uint64
    :param sink: uint64
    :return: m x 2 array of uint64s
        edges that should be removed
    """

    time_start = time.time()

    weighted_graph = nx.Graph()
    weighted_graph.add_edges_from(edges)

    for i_edge, edge in enumerate(edges):
        weighted_graph[edge[0]][edge[1]]['weight'] = affs[i_edge]

    dt = time.time() - time_start
    print("Graph creation: %.2fms" % (dt * 1000))
    time_start = time.time()

    # cutset = nx.minimum_edge_cut(weighted_graph, source, sink)
    cutset = nx.minimum_edge_cut(weighted_graph, source, sink,
                                 flow_func=shortest_augmenting_path)

    dt = time.time() - time_start
    print("Mincut: %.2fms" % (dt * 1000))

    if cutset is None:
        return []

    time_start = time.time()

    weighted_graph.remove_edges_from(cutset)
    print(len(list(nx.connected_components(weighted_graph))))

    dt = time.time() - time_start
    print("Test: %.2fms" % (dt * 1000))

    return np.array(list(cutset), dtype=np.uint64)


class ChunkedGraph(object):
    def __init__(self, table_id, instance_id="pychunkedgraph",
                 project_id="neuromancer-seung-import",
                 chunk_size=(512, 512, 64), fan_out=None, n_layers=None,
                 credentials=None, is_new=False):

        self._client = bigtable.Client(project=project_id, admin=True,
                                       credentials=credentials)
        self._instance = self.client.instance(instance_id)
        self._table_id = table_id

        self._table = self.instance.table(self.table_id)

        if is_new:
            self.check_and_create_table()

        self._n_layers = self.check_and_write_table_parameters("n_layers",
                                                               n_layers)
        self._fan_out = self.check_and_write_table_parameters("fan_out",
                                                              fan_out)
        self._chunk_size = np.array(chunk_size)
        self._bitmasks = compute_bitmasks(self.n_layers, self.fan_out)

        self._n_bits_for_layer_id = 8

    @property
    def client(self):
        return self._client

    @property
    def instance(self):
        return self._instance

    @property
    def table(self):
        return self._table

    @property
    def table_id(self):
        return self._table_id

    @property
    def family_id(self):
        return "0"

    @property
    def fan_out(self):
        return self._fan_out

    @property
    def chunk_size(self):
        return self._chunk_size

    @property
    def n_layers(self):
        return self._n_layers

    @property
    def bitmasks(self):
        return self._bitmasks

    def check_and_create_table(self):
        " Checks if table exists and creates new one if necessary "
        table_ids = [t.table_id for t in self.instance.list_tables()]

        if not self.table_id in table_ids:
            self.table.create()
            f = self.table.column_family(self.family_id)
            f.create()
            print("Table created")

    def check_and_write_table_parameters(self, param_key, value=None):
        """ Checks if a parameter already exists in the table. If it already
        exists it returns the stored value, else it stores the given value. It
        raises an exception if no value is passed and the parameter does not
        exist, yet.

        :param param_key: str
        :param value: uint64
        :return: uint64
            value
        """
        ser_param_key = serialize_key(param_key)
        row = self.table.read_row(serialize_key("params"))

        if row is None or ser_param_key not in row.cells[self.family_id]:
            assert value is not None

            val_dict = {param_key: np.array(value, dtype=np.uint64).tobytes()}
            row = self.mutate_row(serialize_key("params"), self.family_id,
                                  val_dict)

            self.bulk_write([row])
        else:
            value = row.cells[self.family_id][ser_param_key][0].value
            value = np.frombuffer(value, dtype=np.uint64)[0]

        return value

    def get_cg_id_from_rg_id(self, atomic_id):
        """ Extracts ChunkedGraph id from RegionGraph id

        :param atomic_id: int
        :return: int
        """
        # There might be multiple chunk ids for a single rag id because
        # rag supervoxels get split at chunk boundaries. Here, only one
        # chunk id needs to be traced to the top to retrieve the
        # agglomeration id that they all belong to

        r = self.table.read_row(serialize_node_id(atomic_id))
        return np.frombuffer(r.cells[self.family_id][serialize_key("cg_id")][0].value,
                             dtype=np.uint64)[0]

    def get_rg_id_from_cg_id(self, atomic_id):
        """ Extracts RegionGraph id from ChunkedGraph id

        :param atomic_id: int
        :return: int
        """
        return self.read_row(atomic_id, "rg_id")[0]

    def get_layer_id(self, node_id=None, node_id_b=None):
        assert node_id is not None or node_id_b is not None

        if node_id is not None:
            node_id_b = to_bitstring(node_id, bit_width=None, endian="little")

        # The layer id occupies the last byte
        # Use big-endian to get the layer id in the correct format
        return to_int(node_id_b[-self._n_bits_for_layer_id:], endian="big")

    def find_next_node_id(self, example_id, as_bits=False):
        """ Finds a unique node id for the given chunk

        :param example_id: int
            chunk id or node id from the chunk
        :param as_bits: bool
        :return: uint64
        """
        example_id_b = to_bitstring(example_id, bit_width=None, endian="little")
        layer_id = self.get_layer_id(node_id_b=example_id_b)

        # Lookup the number of bits reserved for the chunk coordiantes
        bits_per_dim = self.bitmasks[layer_id]

        # Pad bin string (resolve problems with leading 8bit 0)
        example_id_b = "".join(["0" * (self._n_bits_for_layer_id + 3 * bits_per_dim - len(example_id_b)), example_id_b])

        chunk_id_size = self._n_bits_for_layer_id + 3 * bits_per_dim
        chunk_id_bits = example_id_b[-chunk_id_size:]
        chunk_id = to_int(example_id_b[-chunk_id_size:], endian="little")

        node_id_size = 64 - chunk_id_size

        # Incrementer row keys start with an "i" followed by the chunk id
        row_key = serialize_key("i%d" % chunk_id)
        append_row = self.table.row(row_key, append=True)
        append_row.increment_cell_value(self.family_id, "counter", 1)

        # This increments the row entry and returns the value AFTER incrementing
        latest_row = append_row.commit()

        node_id = int.from_bytes(latest_row[self.family_id][serialize_key('counter')][0][0], byteorder="big")

        # x, y, z = self.get_coordinates_from_chunk_id(chunk_id)
        # print("Next node id in %d / [%d, %d, %d, %d]: %d" % (chunk_id, x, y, z, layer_id, node_id))

        node_id_bits = to_bitstring(node_id, bit_width=node_id_size)

        combined_id_bits = "".join([node_id_bits, chunk_id_bits])

        return to_int(combined_id_bits, endian="little")

    def combine_node_id_chunk_id(self, node_id, chunk_id):
        chunk_id_b = to_bitstring(chunk_id, bit_width=None)

        # The layer id occupies the last byte
        layer_id = self.get_layer_id(node_id_b=chunk_id_b)

        # Lookup the number of bits reserved for the chunk coordiantes
        bits_per_dim = self.bitmasks[layer_id]

        # Pad bin string (resolve problems with leading 8bit 0)
        example_id_b = "".join(["0" * (self._n_bits_for_layer_id +
                                       3 * bits_per_dim - len(chunk_id_b)),
                                chunk_id_b])

        chunk_id_size = self._n_bits_for_layer_id + 3 * bits_per_dim
        chunk_id_bits = example_id_b[-chunk_id_size:]

        node_id_size = 64 - chunk_id_size

        node_id_bits = to_bitstring(node_id, bit_width=node_id_size)

        combined_id_bits = "".join([node_id_bits, chunk_id_bits])

        return to_int(combined_id_bits, endian="little")

    def get_chunk_id_from_node_id(self, node_id, as_bits=False, full=False):
        """ Extracts l, x, y, z

        :param node_id: int
        :param as_bits: bool
        :param full: bool
        :return: list of ints
        """
        node_id_b = to_bitstring(node_id, bit_width=None, endian="little")

        layer_id = self.get_layer_id(node_id_b=node_id_b)

        chunk_id_dim_step = int(self.fan_out ** np.max([0, layer_id - 2]))

        # Lookup the number of bits reserved for the chunk coordiantes
        bits_per_dim = self.bitmasks[layer_id]

        # Pad bin string (resolve problems with leading 8bit 0)
        n_pad = (self._n_bits_for_layer_id + 3 * bits_per_dim - len(node_id_b))
        node_id_b = "".join(["0" * n_pad, node_id_b])

        if full:
            chunk_id_size = self._n_bits_for_layer_id + 3 * bits_per_dim

            if as_bits:
                return node_id_b[-chunk_id_size:]
            else:
                return to_int(node_id_b[-chunk_id_size:], endian="little")
        else:
            if as_bits:
                coords = [node_id_b[-self._n_bits_for_layer_id:]]
            else:
                coords = [layer_id]

            offset = self._n_bits_for_layer_id
            for i_dim in range(3):
                if as_bits:
                    coords.append(node_id_b[- (offset + bits_per_dim): -offset])
                else:
                    coords.append(to_int(node_id_b[- (offset + bits_per_dim): -offset],
                                         endian="big") * chunk_id_dim_step)

                offset += bits_per_dim

            return np.array(coords)

    def get_coordinates_from_chunk_id(self, chunk_id):
        """ Extracts coordinate from chunk id (x, y, z)

        :param chunk_id: int
        :return: list of three ints
        """

        return self.get_chunk_id_from_node_id(chunk_id, full=False)[1:]

    def get_chunk_id_from_coordinates(self, x, y, z, layer_id, bit_width=None):
        """ Creates chunk id from coordinates

        :param x: int
        :param y: int
        :param z: int
        :param layer_id: int
        :return: int
            chunk id
        """
        bits_per_dim = self.bitmasks[layer_id]
        chunk_id_dim_step = int(self.fan_out ** np.max([0, layer_id - 2]))

        if x % chunk_id_dim_step != 0:
            print("Wrong stride in x. coord: [%d, %d, %d], stride: %d" % (x, y, z, chunk_id_dim_step))
        if y % chunk_id_dim_step != 0:
            print("Wrong stride in y. coord: [%d, %d, %d], stride: %d" % (x, y, z, chunk_id_dim_step))
        if z % chunk_id_dim_step != 0:
            print("Wrong stride in z. coord: [%d, %d, %d], stride: %d" % (x, y, z, chunk_id_dim_step))

        # Convert chunk id components to bits
        x_b = to_bitstring(int(x / chunk_id_dim_step), bit_width=bits_per_dim,
                           endian="big")
        y_b = to_bitstring(int(y / chunk_id_dim_step), bit_width=bits_per_dim,
                           endian="big")
        z_b = to_bitstring(int(z / chunk_id_dim_step), bit_width=bits_per_dim,
                           endian="big")
        layer_id_b = to_bitstring(layer_id, bit_width=self._n_bits_for_layer_id,
                                  endian="big")

        chunk_id_b = "".join([z_b, y_b, x_b, layer_id_b])

        return to_int(chunk_id_b, endian="little")

    def get_chunk_ids_from_node_ids(self, node_ids):
        """ Extracts z, y, x, l

        :param node_ids: array of ints
        :return: list of ints
        """
        b = np.unpackbits(np.frombuffer(node_ids, dtype=np.uint8).reshape(len(node_ids), -1), axis=-1)

        layer_ids = np.unique(np.frombuffer(np.packbits(b[:, -self._n_bits_for_layer_id:], axis=-1), dtype=np.uint8))

        assert len(layer_ids) == 1
        layer_id = layer_ids[0]

        bits_per_dim = self.bitmasks[layer_id]
        chunk_ids_b = b[:, -(self._n_bits_for_layer_id + 3 * bits_per_dim):]

        padded_chunk_ids_b = np.pad(chunk_ids_b, [[0, 0], [64 - chunk_ids_b.shape[1], 0]], mode="constant", constant_values=0)
        return np.frombuffer(np.packbits(padded_chunk_ids_b, axis=-1), dtype=np.uint64)

    def read_row(self, node_id, key, idx=0, dtype=np.uint64,
                 get_time_stamp=False):
        """ Reads row from BigTable and takes care of serializations

        :param node_id: uint64
        :param key: table column
        :param idx: column list index
        :param dtype: np.dtype
        :param get_time_stamp: bool
        :return: row entry
        """
        row = self.table.read_row(serialize_node_id(node_id))
        key = serialize_key(key)

        if key not in row.cells[self.family_id]:
            return None

        cell_entries = row.cells[self.family_id][key]
        cell_value = np.frombuffer(cell_entries[idx].value, dtype=dtype)

        if get_time_stamp:
            return cell_value, cell_entries[idx].timestamp
        else:
            return cell_value

    def read_rows(self, node_ids, key, idx=0, dtype=np.uint64):
        """ Applies read_row to many ids

        :param node_ids: list of uint64
        :param key: table column
        :param idx: column list index
        :param dtype: datatype
        :return: row entry
        """
        results = []

        for node_id in node_ids:
            results.append(self.read_row(node_id, key, idx, dtype))

        return results

    def mutate_row(self, row_key, column_family_id, val_dict, time_stamp=None):
        """ Mutates a single row

        :param row_key: serialized bigtable row key
        :param column_family_id: str
            serialized column family id
        :param val_dict: dict
        :param time_stamp: None or datetime
        :return: list
        """
        row = self.table.row(row_key)

        for column, value in val_dict.items():
            row.set_cell(column_family_id=column_family_id, column=column,
                         value=value, timestamp=time_stamp)
        return row

    def bulk_write(self, rows, slow_retry=True):
        """

        :param rows: list
            list of mutated rows
        """
        if slow_retry:
            initial = 5
        else:
            initial = 1

        retry_policy = Retry(
            predicate=if_exception_type((Aborted,
                                         DeadlineExceeded,
                                         ServiceUnavailable)),
            initial=initial,
            maximum=15.0,
            multiplier=2.0,
            deadline=60.0 * 5.0)

        status = self.table.mutate_rows(rows, retry=retry_policy)

        if not any(status):
            raise Exception(status)

    def range_read_chunk(self, x, y, z, layer_id, n_retries=10):
        """ Reads all ids within a chunk

        :param x: int
        :param y: int
        :param z: int
        :param layer_id: int
        :return: list of rows
        """
        bits_per_dim = self.bitmasks[layer_id]

        chunk_id = self.get_chunk_id_from_coordinates(x, y, z, layer_id, bit_width=64)
        chunk_id_b = to_bitstring(chunk_id)[- (bits_per_dim * 3 + self._n_bits_for_layer_id):]
        chunk_id_size = len(chunk_id_b)

        # Take a step in the fast changing dimension (z) - the step size
        # depends on the layer
        step = int(self.fan_out ** np.max([0, layer_id - 2]))

        # if z + step < 2**bits_per_dim:
        #     z_next = z + step
        #     y_next = y
        #     x_next = x
        #     layer_id_next = layer_id
        # elif y + step < 2**bits_per_dim:
        #     z_next = 2**bits_per_dim - 1
        #     y_next = y + 1
        #     x_next = x
        #     layer_id_next = layer_id
        # elif x + step < 2**bits_per_dim:
        #     z_next = 2**bits_per_dim - 1
        #     y_next = 2**bits_per_dim - 1
        #     x_next = x + 1
        #     layer_id_next = layer_id
        # else:
        #     z_next = 0
        #     y_next = 0
        #     x_next = 0
        #     layer_id_next = layer_id + 1

        # print(x, x_next, y, y_next, z, z_next, layer_id, layer_id_next)

        # next_chunk_id = self.get_chunk_id_from_coordinates(x_next,
        #                                                    y_next,
        #                                                    z_next,
        #                                                    layer_id_next,
        #                                                    bit_width=64)

        chunk_id_b_inc = bitwise_inc(chunk_id_b, from_start=True)

        # Define BigTable keys
        start_key = serialize_node_id(chunk_id)
        end_key = serialize_node_id(to_int(chunk_id_b_inc, endian="little"))

        # Set up read
        range_read = self.table.read_rows(start_key=start_key,
                                          end_key=end_key,
                                          end_inclusive=False)

        # Execute read
        consume_success = False
        i_tries = 0
        while not consume_success and i_tries < n_retries:
            range_read.consume_all()
            consume_success = True
            i_tries += 1

        return range_read.rows

    # def range_read_layer(self, layer_id):
    #     """ Reads all ids within a layer
    #
    #     This can take a while depending on the size of the graph
    #
    #     :param layer_id: int
    #     :return: list of rows
    #     """
    #     chunk_id = self.get_chunk_id_from_coordinates(0, 0, 0, layer_id)
    #
    #     if layer_id + 1 not in self.bitmasks:
    #         bits_per_dim = self.bitmasks[layer_id]
    #
    #         x_next = 2**bits_per_dim
    #         y_next = 2**bits_per_dim
    #         z_next = 2**bits_per_dim
    #         layer_id_next = layer_id
    #     else:
    #         x_next = 0
    #         y_next = 0
    #         z_next = 0
    #         layer_id_next = layer_id + 1
    #
    #     next_chunk_id = self.get_chunk_id_from_coordinates(x_next,
    #                                                        y_next,
    #                                                        z_next,
    #                                                        layer_id_next)
    #
    #     print(chunk_id, next_chunk_id)
    #
    #     # Define BigTable keys
    #     start_key = serialize_node_id(chunk_id)
    #     end_key = serialize_node_id(next_chunk_id)
    #
    #     # Set up readssh
    #     range_read = self.table.read_rows(start_key=start_key,
    #                                       end_key=end_key,
    #                                       end_inclusive=False)
    #
    #     # Execute read
    #     range_read.consume_all()
    #
    #     return range_read.rows

    def test_if_nodes_are_in_same_chunk(self, node_ids):
        """ Test whether two nodes are in the same chunk

        :param node_ids: list of two ints
        :return: bool
        """
        assert len(node_ids) == 2

        return self.get_chunk_id_from_node_id(node_ids[0], full=True) ==\
               self.get_chunk_id_from_node_id(node_ids[1], full=True)

    def add_atomic_edges_in_chunks(self, edge_ids, cross_edge_ids, edge_affs,
                                   cross_edge_affs, isolated_node_ids,
                                   cg2rg_dict, rg2cg_dict,
                                   verbose=False, time_stamp=None):
        """ Creates atomic nodes in first abstraction layer for a SINGLE chunk

        Alle edges (edge_ids) need to be from one chunk and no nodes should
        exist for this chunk prior to calling this function. All cross edges
        (cross_edge_ids) have to point out the chunk (first entry is the id
        within the chunk)

        :param edge_ids: n x 2 array of uint64s
        :param cross_edge_ids: m x 2 array of uint64s
        :param edge_affs: float array of length n
        :param cross_edge_affs: float array of length m
        :param isolated_node_ids: list of uint64s
            ids of nodes that have no edge in the chunked graph
        :param cg2rg_dict: dict
        :param rg2cg_dict: dict
        :param verbose: bool
        :param time_stamp: datetime
        """
        if time_stamp is None:
            time_stamp = datetime.datetime.now()

        if time_stamp.tzinfo is None:
            time_stamp = UTC.localize(time_stamp)

        # Catch trivial case
        if edge_ids.size == 0 and cross_edge_ids.size == 0 and len(isolated_node_ids) == 0:
            return 0

        # # Write rg2cg mapping to table
        # rows = []
        # for rg_id in rg2cg_dict.keys():
        #     # Create node
        #     val_dict = {"cg_id": np.array([rg2cg_dict[rg_id]]).tobytes()}
        #
        #     rows.append(self.mutate_row(serialize_node_id(rg_id),
        #                                 self.family_id, val_dict,
        #                                 time_stamp=time_stamp))
        # self.bulk_write(rows)

        # Make parent id creation easier
        if edge_ids.size > 0:
            chunk_id_c = self.get_coordinates_from_chunk_id(edge_ids[0, 0])
        elif cross_edge_ids.size > 0:
            chunk_id_c = self.get_coordinates_from_chunk_id(cross_edge_ids[0, 0])
        else:
            chunk_id_c = self.get_coordinates_from_chunk_id(isolated_node_ids[0])

        parent_chunk_id = self.get_chunk_id_from_coordinates(chunk_id_c[0], chunk_id_c[1], chunk_id_c[2], 2)
        # parent_id_base = np.frombuffer(np.array([0, 0, 0, 0, z, y, x, l+1], dtype=np.uint8), dtype=np.uint32)

        # Get connected component within the chunk
        chunk_g = nx.from_edgelist(edge_ids)
        chunk_g.add_nodes_from(np.unique(cross_edge_ids[:, 0]))
        chunk_g.add_nodes_from(np.unique(isolated_node_ids))
        ccs = list(nx.connected_components(chunk_g))

        # print("%d ccs detected" % (len(ccs)))

        # Add rows for nodes that are in this chunk
        # a connected component at a time
        node_c = 0  # Just a counter for the print / speed measurement
        time_start = time.time()
        for i_cc, cc in enumerate(ccs):
            if verbose and node_c > 0:
                dt = time.time() - time_start
                print("%5d at %5d - %.5fs             " %
                      (i_cc, node_c, dt / node_c), end="\r")

            rows = []

            node_ids = np.array(list(cc))

            # Create parent id
            # parent_id = parent_id_base.copy()
            # parent_id[0] = i_cc
            # parent_id = np.frombuffer(parent_id, dtype=np.uint64)
            parent_id = self.find_next_node_id(parent_chunk_id)
            parent_id_b = np.array(parent_id, dtype=np.uint64).tobytes()

            parent_cross_edges = np.array([], dtype=np.uint64).reshape(0, 2)

            # Add rows for nodes that are in this chunk
            for i_node_id, node_id in enumerate(node_ids):
                # print("Node:", node_id)
                # Extract edges relevant to this node
                edge_col1_mask = edge_ids[:, 0] == node_id
                edge_col2_mask = edge_ids[:, 1] == node_id

                # Cross edges are ordered to always point OUT of the chunk
                cross_edge_mask = cross_edge_ids[:, 0] == node_id

                parent_cross_edges = np.concatenate([parent_cross_edges,
                                                     cross_edge_ids[cross_edge_mask]])

                connected_partner_ids = np.concatenate([edge_ids[edge_col1_mask][:, 1],
                                                        edge_ids[edge_col2_mask][:, 0],
                                                        cross_edge_ids[cross_edge_mask][:, 1]]).tobytes()

                connected_partner_affs = np.concatenate([edge_affs[np.logical_or(edge_col1_mask, edge_col2_mask)],
                                                         cross_edge_affs[cross_edge_mask]]).tobytes()

                # Create node
                val_dict = {"atomic_partners": connected_partner_ids,
                            "atomic_affinities": connected_partner_affs,
                            "parents": parent_id_b,
                            "rg_id": np.array([cg2rg_dict[node_id]]).tobytes()}

                rows.append(self.mutate_row(serialize_node_id(node_id),
                                            self.family_id, val_dict,
                                            time_stamp=time_stamp))
                node_c += 1

            # Create parent node
            val_dict = {"children": node_ids.tobytes(),
                        "atomic_cross_edges": parent_cross_edges.tobytes()}

            rows.append(self.mutate_row(serialize_node_id(parent_id),
                                        self.family_id, val_dict,
                                        time_stamp=time_stamp))

            node_c += 1

            self.bulk_write(rows)

        if verbose:
            try:
                dt = time.time() - time_start
                print("Average time: %.5fs / node; %.5fs / edge - Number of edges: %6d, %6d" %
                      (dt / node_c, dt / len(edge_ids), len(edge_ids), len(cross_edge_ids)))
            except:
                print("WARNING: NOTHING HAPPENED")

    def add_layer(self, layer_id, child_chunk_coords, verbose=False,
                  time_stamp=None, n_threads=20):
        """ Creates the abstract nodes for a given chunk in a given layer

        :param layer_id: int
        :param child_chunk_coords: int array of length 3
            coords in chunk space
        :param verbose: bool
        :param time_stamp: datetime
        """
        def _resolve_cross_chunk_edges_thread(args):
            start, end = args
            for i_child_key, child_key in enumerate(atomic_partner_id_dict_keys[start: end]):
                this_atomic_partner_ids = atomic_partner_id_dict[child_key]
                this_atomic_child_ids = atomic_child_id_dict[child_key]

                leftover_mask = ~np.in1d(this_atomic_partner_ids, u_atomic_child_ids)
                leftover_atomic_edges[child_key] = np.concatenate([this_atomic_child_ids[leftover_mask, None],
                                                                   this_atomic_partner_ids[leftover_mask, None]], axis=1)

                partners = np.unique(child_ids[np.in1d(atomic_child_ids, this_atomic_partner_ids)])

                if len(partners) > 0:
                    these_edges = np.concatenate([np.array([child_key] * len(partners), dtype=np.uint64)[:, None], partners[:, None]], axis=1)
                    edge_ids.extend(these_edges)

        def _write_out_connected_components(args):
            start, end = args
            for i_cc, cc in enumerate(ccs[start: end]):
                    rows = []

                    node_ids = np.array(list(cc))

                    parent_id = self.find_next_node_id(chunk_id)
                    parent_id_b = np.array(parent_id, dtype=np.uint64).tobytes()

                    parent_cross_edges = np.array([], dtype=np.uint64).reshape(0, 2)

                    # Add rows for nodes that are in this chunk
                    for i_node_id, node_id in enumerate(node_ids):
                        # Extract edges relevant to this node
                        parent_cross_edges = np.concatenate([parent_cross_edges,
                                                             leftover_atomic_edges[
                                                                 node_id]])

                        # Create node
                        val_dict = {"parents": parent_id_b}

                        rows.append(self.mutate_row(serialize_node_id(node_id),
                                                    self.family_id, val_dict,
                                                    time_stamp=time_stamp))

                    # Create parent node
                    val_dict = {"children": node_ids.tobytes(),
                                "atomic_cross_edges": parent_cross_edges.tobytes()}

                    rows.append(self.mutate_row(serialize_node_id(parent_id),
                                                self.family_id, val_dict,
                                                time_stamp=time_stamp))

                    self.bulk_write(rows)

        if time_stamp is None:
            time_stamp = datetime.datetime.now()

        if time_stamp.tzinfo is None:
            time_stamp = UTC.localize(time_stamp)

        # 1 ----------
        # The first part is concerned with reading data from the child nodes
        # of this layer and pre-processing it for the second part

        atomic_child_ids = np.array([], dtype=np.uint64)    # ids in lowest layer
        child_ids = np.array([], dtype=np.uint64)   # ids in layer one below this one
        atomic_partner_id_dict = {}
        atomic_child_id_dict = {}

        leftover_atomic_edges = {}

        for chunk_coord in child_chunk_coords:
            # Get start and end key
            x, y, z = chunk_coord

            range_read = self.range_read_chunk(x, y, z, layer_id-1)

            # Loop through nodes from this chunk
            for row_key, row_data in range_read.items():
                row_key = deserialize_node_id(row_key)

                atomic_edges = np.frombuffer(row_data.cells[self.family_id][serialize_key("atomic_cross_edges")][0].value, dtype=np.uint64).reshape(-1, 2)
                atomic_partner_id_dict[int(row_key)] = atomic_edges[:, 1]
                atomic_child_id_dict[int(row_key)] = atomic_edges[:, 0]

                atomic_child_ids = np.concatenate([atomic_child_ids, atomic_edges[:, 0]])
                child_ids = np.concatenate([child_ids, np.array([row_key] * len(atomic_edges[:, 0]), dtype=np.uint64)])

            # print(chunk_coord, start_key, end_key, np.unique(self.get_chunk_ids_from_node_ids(atomic_child_ids)))

        # Extract edges from remaining cross chunk edges
        # and maintain unused cross chunk edges
        edge_ids = []
        u_atomic_child_ids = np.unique(atomic_child_ids)
        atomic_partner_id_dict_keys = np.array(list(atomic_partner_id_dict.keys()), dtype=np.uint64)

        if n_threads > 1:
            n_jobs = n_threads * 3
        else:
            n_jobs = 1

        spacing = np.linspace(0, len(atomic_partner_id_dict_keys), n_jobs+1).astype(np.int)
        starts = spacing[:-1]
        ends = spacing[1:]

        multi_args = list(zip(starts, ends))

        mu.multithread_func(_resolve_cross_chunk_edges_thread, multi_args,
                            n_threads=n_threads)

        # 2 ----------
        # The second part finds connected components, writes the parents to
        # BigTable and updates the childs

        # Make parent id creation easier
        x, y, z = np.min(child_chunk_coords, axis=0)
        chunk_id = self.get_chunk_id_from_coordinates(x, y, z, layer_id)
        # parent_id_base = np.frombuffer(np.array([0, 0, 0, 0, z, y, x, layer_id], dtype=np.uint8), dtype=np.uint32)

        # Extract connected components
        chunk_g = nx.from_edgelist(edge_ids)
        # chunk_g.add_nodes_from(atomic_partner_id_dict_keys)

        # Add single node objects that have no edges
        add_ccs = []
        for node_id in atomic_partner_id_dict_keys[~np.in1d(atomic_partner_id_dict_keys, np.unique(edge_ids))]:
            add_ccs.append([node_id])

        ccs = list(nx.connected_components(chunk_g)) + add_ccs

        # Add rows for nodes that are in this chunk
        # a connected component at a time

        spacing = np.linspace(0, len(ccs), n_jobs+1).astype(np.int)
        starts = spacing[:-1]
        ends = spacing[1:]

        multi_args = list(zip(starts, ends))

        mu.multithread_func(_write_out_connected_components, multi_args,
                            n_threads=n_threads)

    def get_parent(self, node_id, get_only_relevant_parent=True,
                   time_stamp=None):
        """ Acquires parent of a node at a specific time stamp

        :param node_id: uint64
        :param get_only_relevant_parent: bool
            True: return single parent according to time_stamp
            False: return n x 2 list of all parents
                   ((parent_id, time_stamp), ...)
        :param time_stamp: datetime or None
        :return: uint64 or None
        """
        if time_stamp is None:
            time_stamp = datetime.datetime.now()

        if time_stamp.tzinfo is None:
            time_stamp = UTC.localize(time_stamp)

        parent_key = serialize_key("parents")
        all_parents = []

        row = self.table.read_row(serialize_node_id(node_id))

        if parent_key in row.cells[self.family_id]:
            for parent_entry in row.cells[self.family_id][parent_key]:
                if get_only_relevant_parent:
                    if parent_entry.timestamp > time_stamp:
                        continue
                    else:
                        return np.frombuffer(parent_entry.value, dtype=np.uint64)[0]
                else:
                    all_parents.append([np.frombuffer(parent_entry.value, dtype=np.uint64)[0],
                                        parent_entry.timestamp])
        else:
            return None

        if len(all_parents) == 0:
            raise Exception("Did not find a valid parent for %d with"
                            " the given time stamp" % node_id)
        else:
            return all_parents

    def get_children(self, node_id):
        """ Returns all children of a node

        :param node_id: uint64
        :return: list of uint64
        """
        return self.read_row(node_id, "children", dtype=np.uint64)

    def get_root(self, atomic_id, collect_all_parents=False,
                 time_stamp=None, is_cg_id=True):
        """ Takes an atomic id and returns the associated agglomeration ids

        :param atomic_id: int
        :param collect_all_parents: bool
        :param time_stamp: None or datetime
        :param is_cg_id: bool
        :return: int
        """
        if time_stamp is None:
            time_stamp = datetime.datetime.now()

            # Give potentially overlapping writes a head start
            time_stamp -= datetime.timedelta(seconds=5)

        if time_stamp.tzinfo is None:
            time_stamp = UTC.localize(time_stamp)

        if not is_cg_id:
            atomic_id = self.get_cg_id_from_rg_id(atomic_id)

        parent_id = atomic_id

        parent_ids = []

        while True:
            # print(parent_id)
            temp_parent_id = self.get_parent(parent_id, time_stamp)
            if temp_parent_id is None:
                break
            else:
                parent_id = temp_parent_id
                parent_ids.append(parent_id)

        if collect_all_parents:
            return parent_ids
        else:
            return parent_id

    def lock_root_loop(self, root_ids, thread_id, max_tries=100,
                       waittime_s=0.5):
        """ Attempts to lock multiple roots at the same time

        :param root_ids: list of uint64
        :param thread_id: uint64
        :param max_tries: int
        :param waittime_s: float
        :return: bool, list of uint64s
            success, latest root ids
        """

        i_try = 0
        while i_try < max_tries:
            lock_acquired = False

            # Collect latest root ids
            new_root_ids = []
            for i_root_id in range(len(root_ids)):
                latest_root_ids = self.get_latest_root_id(root_ids[i_root_id])

                new_root_ids.extend(latest_root_ids)

            # Attempt to lock all latest root ids
            root_ids = new_root_ids
            for i_root_id in range(len(root_ids)):
                lock_acquired = self.lock_single_root(root_ids[i_root_id],
                                                      thread_id)

                # Roll back locks if one root cannot be locked
                if not lock_acquired:
                    for j_root_id in range(i_root_id):
                        self.unlock_root(root_ids[j_root_id])
                    break

            if lock_acquired:
                return True, root_ids

            time.sleep(waittime_s)
            i_try += 1
            print(i_try)

        return False, root_ids

    def lock_single_root(self, root_id, thread_id):
        """ Attempts to lock the latest version of a root node

        :param root_id: uint64
        :param thread_id: uint64
            an id that is unique to the process asking to lock the root node
        :return: bool
            success
        """
        thread_id = np.array(thread_id, dtype=np.uint64)
        thread_id_b = thread_id.tobytes()

        lock_key = "lock"

        # Check root row
        rr = self.read_row(root_id, lock_key, idx=0, dtype=np.uint64,
                           get_time_stamp=True)

        unlocked = False
        if rr is None:
            # Noe lock
            unlocked = True
        else:
            # Check if lock expired
            dt = UTC.localize(datetime.datetime.now()) - rr[1]
            if dt < LOCK_EXPIRED_TIME_DELTA:
                self.unlock_root(root_id)
                unlocked = True

        # Try to set your own lock
        lock_acquired = False
        if unlocked:
            time_stamp = datetime.datetime.now()
            time_stamp = UTC.localize(time_stamp)

            val_dict = {lock_key: thread_id_b}
            rows = [self.mutate_row(serialize_node_id(root_id),
                                    self.family_id, val_dict,
                                    time_stamp=time_stamp)]
            self.bulk_write(rows)

            # Check if locking was successful
            locked_thread_id = self.read_row(root_id, lock_key, idx=-1,
                                             dtype=np.uint64)
            if locked_thread_id == thread_id:
                lock_acquired = True

        return lock_acquired

    def unlock_root(self, root_id):
        """ Unlocks a root

        This is mainly used for cases where multiple roots need to be locked and
        locking was not sucessful for all of them

        :param root_id: uint64
        """
        row = self.table.row(serialize_node_id(root_id))
        row.delete_cell(self.family_id, serialize_key("lock"))
        row.commit()

    def get_latest_root_id(self, root_id):
        """ Returns the latest root id associated with the provided root id

        :param root_id: uint64
        :return: list of uint64s
        """

        id_working_set = [root_id]
        new_parent_key = serialize_key("new_parents")
        latest_root_ids = []

        while len(id_working_set) > 0:

            next_id = id_working_set[0]
            del(id_working_set[0])
            r = self.table.read_row(serialize_node_id(next_id))

            # Check if a new root id was attached to this root id
            if new_parent_key in r.cells[self.family_id]:
                id_working_set.extend(np.frombuffer(r.cells[self.family_id][new_parent_key][0].value, dtype=np.uint64))
            else:
                latest_root_ids.append(next_id)

        return np.unique(latest_root_ids)

    def read_agglomeration_id_history(self, agglomeration_id, time_stamp=None):
        """ Returns all agglomeration ids agglomeration_id was part of

        :param agglomeration_id: int
        :param time_stamp: None or datetime
            restrict search to ids created after this time_stamp
            None=search whole history
        :return: array of int
        """
        if time_stamp is None:
            time_stamp = datetime.datetime.min

        if time_stamp.tzinfo is None:
            time_stamp = UTC.localize(time_stamp)

        id_working_set = np.array([agglomeration_id], dtype=np.uint64)
        visited_ids = []
        id_history = [agglomeration_id]

        former_parent_key = serialize_key("former_parents")
        new_parent_key = serialize_key("new_parents")

        i = 0
        while len(id_working_set) > 0:
            i += 1

            next_id = id_working_set[0]
            visited_ids.append(id_working_set[0])

            # Get current row
            r = self.table.read_row(serialize_node_id(next_id))

            # Check if there is a newer parent and append
            if new_parent_key in r.cells[self.family_id]:
                new_parent_ids = np.frombuffer(r.cells[self.family_id][new_parent_key][0].value, dtype=np.uint64)

                id_working_set = np.concatenate([id_working_set, new_parent_ids])
                id_history.extend(new_parent_ids)

            # Check if there is an older parent and append if not too old
            if former_parent_key in r.cells[self.family_id]:
                if time_stamp < r.cells[self.family_id][former_parent_key][0].timestamp:
                    former_parent_ids = np.frombuffer(r.cells[self.family_id][former_parent_key][0].value, dtype=np.uint64)

                    id_working_set = np.concatenate([id_working_set, former_parent_ids])
                    id_history.extend(former_parent_ids)

            id_working_set = id_working_set[~np.in1d(id_working_set, visited_ids)]

        return np.unique(id_history)

    def get_subgraph(self, agglomeration_id, bounding_box=None,
                     bb_is_coordinate=False,
                     stop_lvl=1, return_rg_ids=False, get_edges=False,
                     n_threads=5, time_stamp=None):
        """ Returns all edges between supervoxels belonging to the specified
            agglomeration id within the defined bouning box

        :param agglomeration_id: int
        :param bounding_box: [[x_l, y_l, z_l], [x_h, y_h, z_h]]
        :param bb_is_coordinate: bool
        :param stop_lvl: int
        :param return_rg_ids: bool
        :param get_edges: bool
        :param time_stamp: datetime or None
        :param n_threads: int
        :return: edge list
        """
        # Helper functions for multithreading
        #TODO: do this more elagantly
        def _handle_subgraph_children_layer2_edges_thread(child_id):
            return self.get_subgraph_chunk(child_id, time_stamp=time_stamp)

        def _handle_subgraph_children_layer2_thread(child_id):
            return self.get_children(child_id)

        def _handle_subgraph_children_higher_layers_thread(child_id):
            this_children = self.get_children(child_id)

            if bounding_box is not None:
                chunk_ids = self.get_chunk_ids_from_node_ids(this_children)
                chunk_ids = np.array([self.get_coordinates_from_chunk_id(c)
                                      for c in np.unique(chunk_ids)])
                chunk_ids = np.array(chunk_ids)

                chunk_id_bounds = np.array([chunk_ids, chunk_ids +
                                            self.fan_out ** np.max([0, (layer - 3)])])

                bound_check = np.array([np.all(chunk_id_bounds[0] <= bounding_box[1], axis=1),
                                        np.all(chunk_id_bounds[1] >= bounding_box[0], axis=1)]).T

                bound_check_mask = np.all(bound_check, axis=1)
                this_children = this_children[bound_check_mask]

            return this_children

        # Make sure that edges are not requested if we should stop on an
        # intermediate level
        assert stop_lvl == 1 or not get_edges

        if time_stamp is None:
            time_stamp = datetime.datetime.now()

        if time_stamp.tzinfo is None:
            time_stamp = UTC.localize(time_stamp)

        if bounding_box is not None:

            if bb_is_coordinate:
                bounding_box = np.array(bounding_box,
                                        dtype=np.float32) / self.chunk_size
                bounding_box[0] = np.floor(bounding_box[0])
                bounding_box[1] = np.ceil(bounding_box[1])
                bounding_box = bounding_box.astype(np.int)
            else:
                bounding_box = np.array(bounding_box, dtype=np.int)

        edges = np.array([], dtype=np.uint64).reshape(0, 2)
        atomic_ids = np.array([], dtype=np.uint64)
        affinities = np.array([], dtype=np.float32)
        child_ids = [agglomeration_id]

        times = []
        time_start = time.time()
        while len(child_ids) > 0:
            new_childs = []
            layer = self.get_chunk_id_from_node_id(child_ids[0])[0]

            if stop_lvl == layer:
                atomic_ids = child_ids
                break

            if layer == 2:
                if get_edges:
                    edges_and_affinities = mu.multithread_func(
                        _handle_subgraph_children_layer2_edges_thread,
                        child_ids, n_threads=n_threads)

                    for edges_and_affinities_pair in edges_and_affinities:
                        affinities = np.concatenate([affinities,
                                                     edges_and_affinities_pair[1]])
                        edges = np.concatenate([edges,
                                                edges_and_affinities_pair[0]])
                else:
                    n_threads = int(np.min([n_threads, np.ceil(len(child_ids) / 10)]))
                    collected_atomic_ids = mu.multithread_func(
                        _handle_subgraph_children_layer2_thread,
                        child_ids, n_threads=n_threads)

                    for this_atomic_ids in collected_atomic_ids:
                        atomic_ids = np.concatenate([atomic_ids,
                                                     this_atomic_ids])
            else:
                for child_id in child_ids:
                    this_children = self.get_children(child_id)

                    if bounding_box is not None:
                        chunk_ids = self.get_chunk_ids_from_node_ids(this_children)
                        chunk_ids = np.array([self.get_coordinates_from_chunk_id(c)
                                              for c in chunk_ids])
                        chunk_ids = np.array(chunk_ids)

                        chunk_id_bounds = np.array([chunk_ids,
                                                    chunk_ids + self.fan_out ** np.max([0, (layer - 3)])])

                        bound_check = np.array([np.all(chunk_id_bounds[0] <= bounding_box[1], axis=1),
                                                np.all(chunk_id_bounds[1] >= bounding_box[0], axis=1)]).T

                        bound_check_mask = np.all(bound_check, axis=1)
                        this_children = this_children[bound_check_mask]

                        # raise()
                    new_childs.extend(this_children)

            child_ids = new_childs

            print("Layer %d: %.3fms" % (layer, (time.time() - time_start) * 1000))
            time_start = time.time()

        if get_edges:
            if return_rg_ids:
                rg_edges = np.zeros_like(edges, dtype=np.uint64)

                for u_id in np.unique(edges):
                    rg_edges[edges == u_id] = self.get_rg_id_from_cg_id(u_id)

                return np.array(rg_edges), affinities
            else:
                return edges, affinities
        else:
            if return_rg_ids:
                rg_atomic_ids = []

                for u_id in np.unique(atomic_ids):
                    rg_atomic_ids.append(self.get_rg_id_from_cg_id(u_id))

                return np.array(rg_atomic_ids)
            else:
                return atomic_ids

    def get_atomic_partners(self, atomic_id, time_stamp=None):
        """ Extracts the atomic partners and affinities for a given timestamp

        :param atomic_id: uitn64
        :param time_stamp: None or datetime
        :return: list of uint64, list of float32
        """
        if time_stamp is None:
            time_stamp = datetime.datetime.now()

        if time_stamp.tzinfo is None:
            time_stamp = UTC.localize(time_stamp)

        edge_key = serialize_key("atomic_partners")
        affinity_key = serialize_key("atomic_affinities")

        partners = np.array([], dtype=np.uint64)
        affinities = np.array([], dtype=np.float32)

        r = self.table.read_row(serialize_node_id(atomic_id))

        # Shortcut for the trivial case that there have been no changes to
        # the edges of this child:
        if len(r.cells[self.family_id][edge_key]) == 0:
            partners = np.frombuffer(
                r.cells[self.family_id][edge_key][0].value, dtype=np.uint64)
            affinities = np.frombuffer(
                r.cells[self.family_id][affinity_key][0].value,
                dtype=np.float32)

        # From new to old: Add partners that are not
        # in the edge list of this child. This assures that more recent
        # changes are prioritized. For each, check if the time_stamp
        # is satisfied.
        # Note: The creator writes one list of partners (edges) and
        # affinities. Each edit makes only small edits (yet), hence,
        # all but the oldest entry are short lists of length ~ 1-10
        for i_edgelist in range(len(r.cells[self.family_id][edge_key])):
            if time_stamp > r.cells[self.family_id][edge_key][i_edgelist].timestamp:
                partner_batch = np.frombuffer(r.cells[self.family_id][edge_key][i_edgelist].value, dtype=np.uint64)
                affinity_batch = np.frombuffer(r.cells[self.family_id][affinity_key][i_edgelist].value, dtype=np.float32)
                partner_batch_m = ~np.in1d(partner_batch, partners)

                partners = np.concatenate([partners, partner_batch[partner_batch_m]])
                affinities = np.concatenate([affinities, affinity_batch[partner_batch_m]])

        # Take care of removed edges (affinity == 0)
        partners_m = affinities > 0
        partners = partners[partners_m]
        affinities = affinities[partners_m]

        return partners, affinities

    def get_subgraph_chunk(self, parent_id, make_unique=True, time_stamp=None,
                           max_n_threads=5):
        """ Takes an atomic id and returns the associated agglomeration ids

        :param parent_id: int
        :param time_stamp: None or datetime
        :param max_n_threads: int
        :return: edge list
        """
        def _read_atomic_partners(child_id_block):
            thread_edges = np.array([], dtype=np.uint64).reshape(0, 2)
            thread_affinities = np.array([], dtype=np.float32)

            for child_id in child_id_block:
                node_edges, node_affinities = self.get_atomic_partners(child_id, time_stamp=time_stamp)

                # If we have edges add them to the chunk global edge list
                if len(node_edges) > 0:
                    # Build n x 2 edge list from partner list
                    node_edges = np.concatenate([np.ones((len(node_edges), 1), dtype=np.uint64) * child_id, node_edges[:, None]], axis=1)

                    thread_edges = np.concatenate([thread_edges, node_edges])
                    thread_affinities = np.concatenate([thread_affinities, node_affinities])

            return thread_edges, thread_affinities

        if time_stamp is None:
            time_stamp = datetime.datetime.now()

        if time_stamp.tzinfo is None:
            time_stamp = UTC.localize(time_stamp)

        child_ids = self.get_children(parent_id)

        # Iterate through all children of this parent and retrieve their edges
        edges = np.array([], dtype=np.uint64).reshape(0, 2)
        affinities = np.array([], dtype=np.float32)

        n_threads = int(np.min([max_n_threads, np.ceil(len(child_ids) / 10)]))

        child_id_blocks = np.array_split(child_ids, n_threads)
        edges_and_affinities = mu.multithread_func(_read_atomic_partners,
                                                   child_id_blocks,
                                                   n_threads=n_threads)

        for edges_and_affinities_pairs in edges_and_affinities:
            edges = np.concatenate([edges, edges_and_affinities_pairs[0]])
            affinities = np.concatenate([affinities, edges_and_affinities_pairs[1]])

        # If requested, remove duplicate edges. Every edge is stored in each
        # participating node. Hence, we have many edge pairs that look
        # like [x, y], [y, x]. We solve this by sorting and calling np.unique
        # row-wise
        if make_unique:
            edges, idx = np.unique(np.sort(edges, axis=1), axis=0,
                                   return_index=True)
            affinities = affinities[idx]

        return edges, affinities

    def add_edge_locked(self, thread_id, atomic_edge, affinity=None,
                        root_ids=None):

        if root_ids is None:
            root_ids = [self.get_root(atomic_edge[0]),
                        self.get_root(atomic_edge[1])]

        if self.lock_root_loop(root_ids=root_ids, thread_id=thread_id)[0]:
            return self.add_edge(atomic_edge=atomic_edge, affinity=affinity)
        else:
            return root_ids

    def add_edge(self, atomic_edge, affinity=None, is_cg_id=True):
        """ Adds an atomic edge to the ChunkedGraph

        :param atomic_edge: list of two ints
        :param affinity: float
        :param is_cg_id: bool
        :return: int
            new root id
        """
        time_stamp = datetime.datetime.now()
        time_stamp = UTC.localize(time_stamp)

        if affinity is None:
            affinity = 1

        rows = []

        if not is_cg_id:
            atomic_edge = [self.get_cg_id_from_rg_id(atomic_edge[0]),
                           self.get_cg_id_from_rg_id(atomic_edge[1])]

        # Walk up the hierarchy until a parent in the same chunk is found
        original_parent_ids = [self.get_root(atomic_edge[0], is_cg_id=True,
                                             collect_all_parents=True),
                               self.get_root(atomic_edge[1], is_cg_id=True,
                                             collect_all_parents=True)]

        original_parent_ids = np.array(original_parent_ids).T

        merge_layer = None
        for i_layer in range(len(original_parent_ids)):
            if self.test_if_nodes_are_in_same_chunk(original_parent_ids[i_layer]):
                merge_layer = i_layer
                break

        if merge_layer is None:
            raise Exception("No parents found. Did you set is_cg_id correctly?")

        original_root = original_parent_ids[-1]

        # Find a new node id and update all children
        # circumvented_nodes = current_parent_ids.copy()
        # chunk_id = self.get_chunk_id_from_node_id(original_parent_ids[merge_layer][0], full=True)

        new_parent_id = self.find_next_node_id(original_parent_ids[merge_layer][0])
        new_parent_id_b = np.array(new_parent_id).tobytes()
        current_node_id = None

        for i_layer in range(merge_layer, len(original_parent_ids)):
            current_parent_ids = original_parent_ids[i_layer]

            # Collect child ids of all nodes --> childs of new node
            if current_node_id is None:
                combined_child_ids = np.array([], dtype=np.uint64)
            else:
                combined_child_ids = np.array([current_node_id], dtype=np.uint64).flatten()

            for prior_parent_id in current_parent_ids:
                child_ids = self.get_children(prior_parent_id)

                # Exclude parent nodes from old hierarchy path
                if i_layer > merge_layer:
                    child_ids = child_ids[~np.in1d(child_ids, original_parent_ids)]

                combined_child_ids = np.concatenate([combined_child_ids,
                                                     child_ids])

                # Append new parent entry for all children
                for child_id in child_ids:
                    val_dict = {"parents": new_parent_id_b}
                    rows.append(self.mutate_row(serialize_node_id(child_id),
                                                self.family_id,
                                                val_dict,
                                                time_stamp=time_stamp))

            # Create new parent node
            val_dict = {"children": combined_child_ids.tobytes()}
            current_node_id = new_parent_id  # Store for later

            if i_layer < len(original_parent_ids) - 1:
                # chunk_id = self.get_chunk_id_from_node_id(original_parent_ids[i_layer + 1][0], full=True)

                new_parent_id = self.find_next_node_id(original_parent_ids[i_layer + 1][0])
                new_parent_id_b = np.array(new_parent_id).tobytes()

                val_dict["parents"] = new_parent_id_b
            else:
                val_dict["former_parents"] = np.array(original_root).tobytes()

                rows.append(self.mutate_row(serialize_node_id(original_root[0]),
                                            self.family_id,
                                            {"new_parents": new_parent_id_b},
                                            time_stamp=time_stamp))

                rows.append(self.mutate_row(serialize_node_id(original_root[1]),
                                            self.family_id,
                                            {"new_parents": new_parent_id_b},
                                            time_stamp=time_stamp))

            # Read original cross chunk edges
            atomic_cross_edges = np.array([], dtype=np.uint64).reshape(0, 2)
            for original_parent_id in original_parent_ids[i_layer]:
                this_atomic_cross_edges = self.read_row(original_parent_id,
                                                        "atomic_cross_edges").reshape(-1, 2)
                atomic_cross_edges = np.concatenate([atomic_cross_edges,
                                                     this_atomic_cross_edges])

            val_dict["atomic_cross_edges"] = atomic_cross_edges.tobytes()

            rows.append(self.mutate_row(serialize_node_id(current_node_id),
                                        self.family_id, val_dict,
                                        time_stamp=time_stamp))

        # Atomic edge
        for i_atomic_id in range(2):
            val_dict = {"atomic_partners": np.array([atomic_edge[(i_atomic_id + 1) % 2]]).tobytes(),
                        "atomic_affinities": np.array([affinity], dtype=np.float32).tobytes()}
            rows.append(self.mutate_row(serialize_node_id(atomic_edge[i_atomic_id]),
                                        self.family_id, val_dict,
                                        time_stamp=time_stamp))

        self.bulk_write(rows, slow_retry=False)

        return new_parent_id

    def remove_edges(self, atomic_edges, is_cg_id=True):
        """ Removes atomic edges from the ChunkedGraph

        :param atomic_edges: list of two uint64s
        :param is_cg_id: bool
        :return: list of uint64s
            new root ids
        """
        time_stamp = datetime.datetime.now()
        time_stamp = UTC.localize(time_stamp)

        # Make sure that we have a list of edges
        if isinstance(atomic_edges[0], np.uint64):
            atomic_edges = [atomic_edges]

        if not is_cg_id:
            for i_atomic_edge in range(len(atomic_edges)):
                atomic_edges[i_atomic_edge] = [self.get_cg_id_from_rg_id(atomic_edges[i_atomic_edge][0]),
                                               self.get_cg_id_from_rg_id(atomic_edges[i_atomic_edge][1])]

        atomic_edges = np.array(atomic_edges)
        u_atomic_ids = np.unique(atomic_edges)

        # Get number of layers and the original root
        original_parent_ids = self.get_root(atomic_edges[0, 0], is_cg_id=True,
                                            collect_all_parents=True)
        n_layers = len(original_parent_ids)
        original_root = original_parent_ids[-1]

        # Find lowest level chunks that might have changed
        chunk_ids = self.get_chunk_ids_from_node_ids(u_atomic_ids)
        u_chunk_ids, u_chunk_ids_idx = np.unique(chunk_ids,
                                                 return_index=True)

        involved_chunk_id_dict = dict(zip(u_chunk_ids, u_atomic_ids[u_chunk_ids_idx]))

        # Note: After removing the atomic edges, we basically need to build the
        # ChunkedGraph for these chunks from the ground up.
        # involved_chunk_id_dict stores a representative for each chunk that we
        # can use to acquire the parent that knows about all atomic nodes in the
        # chunk.

        # Remove atomic edges
        rows = []

        # Removing edges nodewise. We cannot remove edges edgewise because that
        # would add up multiple changes to each node (row). Unfortunately,
        # the batch write (mutate_rows) from BigTable cannot handle multiple
        # changes to the same row within a batch write and only executes
        # one of them.
        for u_atomic_id in np.unique(atomic_edges):
            partners = np.concatenate([atomic_edges[atomic_edges[:, 0] == u_atomic_id][:, 1],
                                       atomic_edges[atomic_edges[:, 1] == u_atomic_id][:, 0]])

            val_dict = {"atomic_partners": partners.tobytes(),
                        "atomic_affinities": np.zeros(len(partners), dtype=np.float32).tobytes()}

            rows.append(self.mutate_row(serialize_node_id(u_atomic_id),
                                        self.family_id, val_dict,
                                        time_stamp=time_stamp))

        # Execute the removal of the atomic edges - we cannot wait for that
        # until the end because we want to compute connected components on the
        # subgraph

        self.bulk_write(rows, slow_retry=False)
        rows = []

        # Dictionaries keeping temporary information about the ChunkedGraph
        # while updates are not written to BigTable yet
        new_layer_parent_dict = {}
        cross_edge_dict = {}
        old_id_dict = collections.defaultdict(list)

        # For each involved chunk we need to compute connected components
        for chunk_id in involved_chunk_id_dict.keys():
            # Get the local subgraph
            node_id = involved_chunk_id_dict[chunk_id]
            old_parent_id = self.get_parent(node_id)
            edges, affinities = self.get_subgraph_chunk(old_parent_id,
                                                        make_unique=False)

            # The cross chunk edges are passed on to the parents to compute
            # connected components in higher layers.

            cross_edge_mask = self.get_chunk_ids_from_node_ids(np.ascontiguousarray(edges[:, 1])) != \
                              self.get_chunk_id_from_node_id(node_id, full=True)

            cross_edges = edges[cross_edge_mask]
            edges = edges[~cross_edge_mask]

            # Build the local subgraph and compute connected components
            G = nx.from_edgelist(edges)
            ccs = nx.connected_components(G)

            # For each connected component we create one new parent
            for cc in ccs:
                cc_node_ids = np.array(list(cc), dtype=np.uint64)

                # Get the associated cross edges
                cc_cross_edges = cross_edges[np.in1d(cross_edges[:, 0],
                                                     cc_node_ids)]

                # Get a new parent id
                new_parent_id = self.find_next_node_id(old_parent_id)
                new_parent_id_b = np.array(new_parent_id).tobytes()
                new_parent_id = new_parent_id

                # Temporarily storing information on how the parents of this cc
                # are changed by the split. We need this information when
                # processing the next layer
                new_layer_parent_dict[new_parent_id] = old_parent_id
                cross_edge_dict[new_parent_id] = cc_cross_edges
                old_id_dict[old_parent_id].append(new_parent_id)

                # Make changes to the rows of the lowest layer
                val_dict = {"children": cc_node_ids.tobytes(),
                            "atomic_cross_edges": cc_cross_edges.tobytes()}

                rows.append(self.mutate_row(serialize_node_id(new_parent_id),
                                            self.family_id, val_dict,
                                            time_stamp=time_stamp))

                for cc_node_id in cc_node_ids:
                    val_dict = {"parents": new_parent_id_b}

                    rows.append(self.mutate_row(serialize_node_id(cc_node_id),
                                                self.family_id, val_dict,
                                                time_stamp=time_stamp))

        # Now that the lowest layer has been updated, we need to walk through
        # all layers and move our new parents forward
        # new_layer_parent_dict stores all newly created parents. We first
        # empty it and then fill it with the new parents in the next layer
        new_roots = []
        for i_layer in range(n_layers - 1):

            parent_cc_list = []
            parent_cc_old_parent_list = []
            parent_cc_mapping = {}
            leftover_edges = {}
            old_parent_dict = {}

            # print(new_layer_parent_dict)
            # print(cross_edge_dict)

            for new_layer_parent in new_layer_parent_dict.keys():
                old_parent_id = new_layer_parent_dict[new_layer_parent]
                cross_edges = cross_edge_dict[new_layer_parent]

                # Using the old parent's parents: get all nodes in the
                # neighboring chunks (go one up and one down in all directions)
                old_next_layer_parent = self.get_parent(old_parent_id)
                old_chunk_neighbors = self.get_children(old_next_layer_parent)
                old_chunk_neighbors = old_chunk_neighbors[old_chunk_neighbors != old_parent_id]

                # z, y, x, l = self.get_chunk_id_from_node_id(old_next_layer_parent)
                # parent_id_base = np.frombuffer(np.array([0, 0, 0, 0, z, y, x, l],
                #                                         dtype=np.uint8),
                #                                dtype=np.uint32)[1]

                old_parent_dict[new_layer_parent] = old_next_layer_parent

                # In analogy to `add_layer`, we need to compare
                # cross_chunk_edges among potential neighbors. Here, we know
                # that all future neighbors are among the old neighbors
                # (old_chunk_neighbors) or their new replacements due to this
                # split.
                atomic_children = cross_edges[:, 0]
                atomic_id_map = np.ones(len(cross_edges), dtype=np.uint64) * \
                                new_layer_parent
                partner_cross_edges = {new_layer_parent: cross_edges}

                for old_chunk_neighbor in old_chunk_neighbors:
                    # For each neighbor we need to check whether this neighbor
                    # was affected by a split as well (and was updated):
                    # neighbor_id in old_id_dict. If so, we take the new atomic
                    # cross edges (temporary data) into account, else, we load
                    # the atomic_cross_edges from BigTable
                    if old_chunk_neighbor in old_id_dict:
                        for new_neighbor in old_id_dict[old_chunk_neighbor]:
                            neigh_cross_edges = cross_edge_dict[new_neighbor]
                            atomic_children = np.concatenate([atomic_children,
                                                              neigh_cross_edges[:, 0]])

                            partner_cross_edges[new_neighbor] = neigh_cross_edges
                            atomic_id_map = np.concatenate([atomic_id_map,
                                                             np.ones(len(neigh_cross_edges), dtype=np.uint64) * new_neighbor])
                    else:
                        neigh_cross_edges = self.read_row(old_chunk_neighbor, "atomic_cross_edges").reshape(-1, 2)
                        atomic_children = np.concatenate([atomic_children,
                                                          neigh_cross_edges[:, 0]])

                        partner_cross_edges[old_chunk_neighbor] = neigh_cross_edges
                        atomic_id_map = np.concatenate([atomic_id_map,
                                                        np.ones(len(neigh_cross_edges), dtype=np.uint64) * old_chunk_neighbor])

                u_atomic_children = np.unique(atomic_children)
                edge_ids = np.array([], dtype=np.uint64).reshape(-1, 2)

                # raise()

                # For each potential neighbor (now, adjusted for changes in
                # neighboring chunks), compare cross edges and extract edges
                # (edge_ids) between them
                for pot_partner in partner_cross_edges.keys():
                    this_atomic_partner_ids = partner_cross_edges[pot_partner][:, 1]

                    this_atomic_child_ids = partner_cross_edges[pot_partner][:, 0]

                    leftover_mask = ~np.in1d(this_atomic_partner_ids,
                                             u_atomic_children)

                    leftover_edges[pot_partner] = np.concatenate(
                        [this_atomic_child_ids[leftover_mask, None],
                         this_atomic_partner_ids[leftover_mask, None]], axis=1)

                    partners = np.unique(atomic_id_map[np.in1d(atomic_children,
                                                               this_atomic_partner_ids)])
                    these_edges = np.concatenate([np.array(
                        [pot_partner] * len(partners), dtype=np.uint64)[:, None],
                                                  partners[:, None]], axis=1)

                    edge_ids = np.concatenate([edge_ids, these_edges])

                # Create graph and run connected components
                chunk_g = nx.from_edgelist(edge_ids)
                chunk_g.add_nodes_from(np.array([new_layer_parent], dtype=np.uint64))
                ccs = list(nx.connected_components(chunk_g))

                # Filter the connected component that is relevant to the
                # current new_layer_parent
                partners = []
                for cc in ccs:
                    if new_layer_parent in cc:
                        partners = cc
                        break

                # Check if the parent has already been "created"
                if new_layer_parent in parent_cc_mapping:
                    parent_cc_id = parent_cc_mapping[new_layer_parent]
                    parent_cc_list[parent_cc_id].extend(partners)
                    parent_cc_list[parent_cc_id].append(new_layer_parent)
                else:
                    parent_cc_id = len(parent_cc_list)
                    parent_cc_list.append(list(partners))
                    parent_cc_list[parent_cc_id].append(new_layer_parent)
                    parent_cc_old_parent_list.append(old_next_layer_parent)

                # Inverse mapping
                for partner_id in partners:
                    parent_cc_mapping[partner_id] = parent_cc_id

            # Create the new_layer_parent_dict for the next layer and write
            # nodes (lazy)
            new_layer_parent_dict = {}
            for i_cc, parent_cc in enumerate(parent_cc_list):
                old_next_layer_parent = None
                for parent_id in parent_cc:
                    if parent_id in old_parent_dict:
                        old_next_layer_parent = old_parent_dict[parent_id]

                assert old_next_layer_parent is not None

                cc_node_ids = np.array(list(parent_cc), dtype=np.uint64)
                cc_cross_edges = np.array([], dtype=np.uint64).reshape(0, 2)

                for parent_id in parent_cc:
                    cc_cross_edges = np.concatenate([cc_cross_edges,
                                                     leftover_edges[parent_id]])

                new_parent_id = self.find_next_node_id(old_next_layer_parent)
                new_parent_id_b = np.array(new_parent_id).tobytes()
                new_parent_id = new_parent_id

                new_layer_parent_dict[new_parent_id] = parent_cc_old_parent_list[i_cc]
                cross_edge_dict[new_parent_id] = cc_cross_edges
                # old_id_dict[old_parent_id].append(new_parent_id)

                for cc_node_id in cc_node_ids:
                    val_dict = {"parents": new_parent_id_b}

                    rows.append(self.mutate_row(serialize_node_id(cc_node_id),
                                                self.family_id, val_dict,
                                                time_stamp=time_stamp))

                val_dict = {"children": cc_node_ids.tobytes(),
                            "atomic_cross_edges": cc_cross_edges.tobytes()}

                if i_layer == n_layers - 2:
                    new_roots.append(new_parent_id)
                    val_dict["former_parents"] = np.array(original_root).tobytes()

                rows.append(self.mutate_row(serialize_node_id(new_parent_id),
                                            self.family_id, val_dict,
                                            time_stamp=time_stamp))

            if i_layer == n_layers - 2:
                rows.append(self.mutate_row(serialize_node_id(original_root),
                                            self.family_id,
                                            {"new_parents": np.array(new_roots, dtype=np.uint64).tobytes()},
                                            time_stamp=time_stamp))

        self.bulk_write(rows, slow_retry=False)
        return new_roots

    def remove_edges_mincut_locked(self, thread_id, source_id, sink_id,
                                   source_coord, sink_coord,
                                   bb_offset=(240, 240, 24), root_ids=None):

        if root_ids is None:
            root_ids = [self.get_root(source_id),
                        self.get_root(sink_id)]

        if root_ids[0] != root_ids[1]:
            return root_ids

        if self.lock_root_loop(root_ids=root_ids[:1], thread_id=thread_id)[0]:
            return self.remove_edges_mincut(source_id=source_id,
                                            sink_id=sink_id,
                                            source_coord=source_coord,
                                            sink_coord=sink_coord,
                                            bb_offset=bb_offset)
        else:
            return root_ids

    def remove_edges_mincut(self, source_id, sink_id, source_coord,
                            sink_coord, bb_offset=(120, 120, 12),
                            is_cg_id=True):
        """ Computes mincut and removes


        :param source_id: uint64
        :param sink_id: uint64
        :param source_coord: list of 3 ints
            [x, y, z] coordinate of source supervoxel
        :param sink_coord: list of 3 ints
            [x, y, z] coordinate of sink supervoxel
        :param bb_offset: list of 3 ints
            [x, y, z] bounding box padding beyond box spanned by coordinates
        :param is_cg_id: bool
        :return: list of uint64s
            new root ids
        """

        time_start = time.time()    # ------------------------------------------

        bb_offset = np.array(list(bb_offset))
        source_coord = np.array(source_coord)
        sink_coord = np.array(sink_coord)

        if not is_cg_id:
            source_id = self.get_cg_id_from_rg_id(source_id)
            sink_id = self.get_cg_id_from_rg_id(sink_id)

        # Decide a reasonable bounding box
        #TODO: improve by iteratively using a bigger context if no path between source and sink exists

        coords = np.concatenate([source_coord[:, None], sink_coord[:, None]],
                                axis=1).T
        bounding_box = [np.min(coords, axis=0), np.max(coords, axis=0)]

        bounding_box[0] -= bb_offset
        bounding_box[1] += bb_offset

        root_id_source = self.get_root(source_id, is_cg_id=True)
        root_id_sink = self.get_root(source_id, is_cg_id=True)

        # Verify that sink and source are from the same root object
        if root_id_source != root_id_sink:
            return [root_id_source, root_id_sink]

        print("Get roots and check: %.3fms" % ((time.time() - time_start)*1000))
        time_start = time.time()    # ------------------------------------------

        root_id = root_id_source

        # Get edges between local supervoxels
        edges, affs = self.get_subgraph(root_id, get_edges=True,
                                        bounding_box=bounding_box,
                                        bb_is_coordinate=True)

        print("Get edges and affs: %.3fms" % ((time.time() - time_start)*1000))
        time_start = time.time()  # ------------------------------------------

        # Compute mincut
        atomic_edges = mincut(edges, affs, source_id, sink_id)

        print("Mincut: %.3fms" % ((time.time() - time_start)*1000))
        time_start = time.time()  # ------------------------------------------

        if len(atomic_edges) == 0:
            print("WARNING: Mincut failed. Try again...")
            return [root_id]

        # Remove edges
        new_roots = self.remove_edges(atomic_edges, is_cg_id=True)
        # new_roots = [agglomeration_id]

        print("Remove edges: %.3fms" % ((time.time() - time_start)*1000))
        time_start = time.time()  # ------------------------------------------

        print(new_roots)

        return new_roots