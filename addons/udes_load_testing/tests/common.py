# -*- coding: utf-8 -*-

import re
import time
import types
from ascii_graph import Pyasciigraph
from collections import defaultdict
from functools import wraps
from parameterized import parameterized     # noqa: F401 (test modules import it from here)
from odoo.addons.udes_stock.tests import common
from odoo.tests.common import SavepointCase, at_install, post_install
from .config import config

def time_func(func):
    # attach an attribute to method
    if not hasattr(func, 'duration'):
        func.__dict__.update({'duration': [None]})

    @wraps(func)
    def _wrapper(*args, **kwargs):
        start = time.time()
        result = func(*args, **kwargs)
        func.duration[0] = (time.time() - start)
        return result
    return _wrapper


def instrument_timings(cls):
    """Decorate a class's timing methods with time_func."""
    for k, v in cls.__dict__.items():
        if isinstance(v, types.FunctionType) and k.startswith('time_'):
            setattr(cls, k, time_func(v))
    return cls


@at_install(False)
@post_install(True)
class LoadRunner(SavepointCase):

    xlabel = 'I should be replaced'
    ylabel = 'Time taken/s'

    @classmethod
    def setUpClass(cls):
        super(LoadRunner, cls).setUpClass()
        if 'Test' not in cls.__name__:
            return None

        if not hasattr(cls, '_filename'):
            cls._filename = '%s_times.tsv' % re.sub('^Test', '', cls.__name__)

        try:
            cls._fw = open(cls._filename, 'a')
        except IOError:
            cls._fw = open(cls._filename, 'w')

        cls._fw.write('%s\t%s\n' % (cls.xlabel, cls.ylabel))

        cls.results = defaultdict(lambda: defaultdict(list))

    def tearDown(self):
        super(LoadRunner, self).tearDown()
        if self._fw:
            self._fw.flush()

    @classmethod
    def tearDownClass(cls):
        super(LoadRunner, cls).tearDownClass()
        if cls._fw:
            cls._fw.close()

    def write_line(self, *args):
        self._fw.write('\t'.join(map(str, args))+'\n')

    def _process_results(self, n, *funcs):
        """ Process the durations of the functions into results"""
        total = 0
        for i, f in enumerate(funcs):
            func_name = re.sub('^time_', '', f.__name__)
            self.results[(i, func_name)][n].append(f.duration[0])
            self.write_line(func_name, n, f.duration[0])
            total += f.duration[0]

        self.write_line('total', n, total)
        self.results[(len(funcs), 'total')][n].append(total)

    def _report(self):
        """Make some nice ascii graphs"""
        graph = Pyasciigraph(
            min_graph_length=80,
            human_readable='si',
        )
        for func_name, res in sorted(self.results.items()):
            plot_data = []
            for key, vals in sorted(res.items()):
                plot_data.append((key ,sum(vals)/len(vals)))

            # Multiply by 1000 to stop truncation for quick calls
            lines = graph.graph(' %s/ms' % func_name[1],
                                [('Mean (N=%i)' % k, m * 1000)
                                for k, m in plot_data])
            for line in lines:
                print(line)



class BackgroundDataRunner(LoadRunner, common.BaseUDES):

    @classmethod
    def setUpClass(cls):
        super(BackgroundDataRunner, cls).setUpClass()
        cls._n = config.get_background_n(cls.__name__)
        cls._dummy_picking_type = cls.picking_type_pick
        cls._dummy_background_data()

    @classmethod
    def _dummy_background_data(cls):
        """Create some dummy background data"""
        Location = cls.env['stock.location']
        Package = cls.env['stock.quant.package']
        Picking = cls.env['stock.picking']

        full_location = Location.create({
            'name': 'TEST DUMMY LOCATION',
            'barcode': 'LTESTDUMMY',
        })

        child_locations = Location.browse()
        pickings = Picking.browse()

        for i in range(cls._n):
            loc = Location.create({
                'name': 'TEST DUMMY LOCATION %0.4i' % i,
                'barcode': 'LTESTDUMMY%0.4i' % i,
            })
            child_locations |= loc

            prod = cls.create_product('DUMMY%0.4i' % i)

            pack = Package.get_package(
                'TEST_DUMMY_%0.4i' % i, create=True
            )

            cls.create_quant(
                product_id=prod.id,
                location_id=loc.id,
                qty=100,
                package_id=pack.id
            )

            pick = cls.create_picking(
                picking_type=cls._dummy_picking_type,
                origin="TEST_DUMMY_origin_%0.4i" % i,
                products_info=[{'product': prod, 'qty': 100}],
            )
            pickings |= pick

            line_end = '\r'
            if i + 1 == cls._n:
                line_end = '\n'

            print('Setting up background data (%0.2f' \
                  % (100*(i+1)/cls._n) + '%)', end=line_end)

        child_locations.write({'location_id': full_location.id})
        full_location.write({'location_id': cls.stock_location.id})
        print('Assigning picks')
        pickings.action_assign()
        print('Complete')
