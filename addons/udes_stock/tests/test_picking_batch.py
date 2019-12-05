# -*- coding: utf-8 -*-

from . import common

from odoo.exceptions import ValidationError
from unittest.mock import patch

class TestGoodsInPickingBatch(common.BaseUDES):

    @classmethod
    def setUpClass(cls):
        super(TestGoodsInPickingBatch, cls).setUpClass()
        cls.env.user.get_user_warehouse().u_remove_unready_batch = True
        cls.env.user.get_user_warehouse().u_auto_assign_batch = True
        cls.pack_4apples_info = [{'product': cls.apple,
                                  'qty': 4}]
        # enable unpickable items by default
        cls.picking_type_pick.u_enable_unpickable_items = True

    def setUp(self):
        super(TestGoodsInPickingBatch, self).setUp()
        Package = self.env['stock.quant.package']

        self.package_one   = Package.get_package("test_package_one", create=True)
        self.package_two   = Package.get_package("test_package_two", create=True)
        self.package_three = Package.get_package("test_package_three", create=True)
        self.package_four  = Package.get_package("test_package_four", create=True)

    def test01_check_user_id_raise_with_empty_id_string(self):
        """ Should error if passed an empty id """
        batch = self.create_batch(user=self.outbound_user)
        batch = batch.sudo(self.outbound_user)

        with self.assertRaises(ValidationError) as err:
            batch._check_user_id("")

        self.assertEqual(err.exception.name, "Cannot determine the user.")

    def test02_check_user_id_valid_id(self):
        """ Should return a non empty string """
        batch = self.create_batch(user=self.outbound_user)
        batch = batch.sudo(self.outbound_user)

        checked_user_id = batch._check_user_id("42")

        self.assertEqual(checked_user_id, "42")

    def test03_check_user_id_default_id(self):
        """ Should return the current user id if passed None """
        batch = self.create_batch(user=self.outbound_user)
        batch = batch.sudo(self.outbound_user)

        user_id = batch._check_user_id(None)

        self.assertEqual(user_id, self.outbound_user.id)

    def test04_get_single_batch_no_batch_no_picking(self):
        """ Should not create anything if no picking exists """
        Batch = self.env['stock.picking.batch']
        Batch = Batch.sudo(self.outbound_user)

        batch = Batch.get_single_batch()

        self.assertIsNone(batch, "Unexpected batch created")

    def test05_get_single_batch_no_batch_one_picking(self):
        """
        Get single batch returns none when no batch has been
        created for the current user.

        """
        Batch = self.env['stock.picking.batch']
        Batch = Batch.sudo(self.outbound_user)

        self.create_quant(self.apple.id, self.test_location_01.id, 4,
                          package_id=self.package_one.id)
        self.create_picking(self.picking_type_pick,
                            products_info=self.pack_4apples_info,
                            confirm=True,
                            assign=True)
        batch = Batch.get_single_batch()

        self.assertIsNone(batch, "Unexpected batch found")

    def test06_get_single_batch_error_multiple_batches(self):
        """
        Should raise an error when the user already has (by
        instrumenting the datastore) multiple batches in the
        'in_progress' state associated with the user.

        """
        Batch = self.env['stock.picking.batch']
        Batch = Batch.sudo(self.outbound_user)

        self.create_quant(self.apple.id, self.test_location_01.id, 4,
                          package_id=self.package_one.id)
        self.create_quant(self.apple.id, self.test_location_01.id, 4,
                          package_id=self.package_two.id)

        batch01 = self.create_batch(user=self.outbound_user)
        self.create_picking(self.picking_type_pick,
                            products_info=self.pack_4apples_info,
                            confirm=True,
                            assign=True,
                            batch_id=batch01.id)
        batch01.state = 'in_progress'

        batch02 = self.create_batch(user=self.outbound_user)
        self.create_picking(self.picking_type_pick,
                            products_info=self.pack_4apples_info,
                            confirm=True,
                            assign=True,
                            batch_id=batch02.id)
        batch02.state = 'in_progress'

        batches = Batch.search([('user_id', '=', self.outbound_user.id),
                                ('state', '=', 'in_progress')])

        # check pre-conditions
        self.assertEqual(len(batches), 2)

        with self.assertRaises(ValidationError) as err:
            Batch.get_single_batch()

        self.assertEqual(
            err.exception.name,
            "Found 2 batches for the user, please contact administrator.")

    def test07_get_single_batch_no_batch_multiple_pickings(self):
        """
        Get single batch returns none when no batch has been
        created for the current user, even having multiple pickings.

        """
        Batch = self.env['stock.picking.batch']
        Package = self.env['stock.quant.package']
        Batch = Batch.sudo(self.outbound_user)

        for idx in range(3):
            pack = Package.get_package("test_package_%d" % idx, create=True)
            self.create_quant(self.apple.id, self.test_location_01.id, 4,
                              package_id=pack.id)
            self.create_picking(self.picking_type_pick,
                                products_info=self.pack_4apples_info,
                                confirm=True,
                                assign=True)

        batch = Batch.get_single_batch()

        self.assertIsNone(batch, "Unexpected batch found")

    def test08_create_batch_with_priorities(self):
        """
        Should create a batch by correctly filtering pickings by
        priority.

        """
        Batch = self.env['stock.picking.batch']
        Package = self.env['stock.quant.package']
        Batch = Batch.sudo(self.outbound_user)

        for idx in range(4):
            pack = Package.get_package("test_package_%d" % idx, create=True)
            self.create_quant(self.apple.id, self.test_location_01.id, 4,
                              package_id=pack.id)
            self.create_picking(self.picking_type_pick,
                                products_info=self.pack_4apples_info,
                                confirm=True,
                                assign=True,
                                priority=str(idx))

        batch = Batch.create_batch(self.picking_type_pick.id, ["2"])

        self.assertIsNotNone(batch, "No batch created")
        self.assertEqual(len(batch.picking_ids), 1,
                         "Multiple pickings were included in the batch")
        self.assertEqual(batch.picking_ids[0].priority, "2",
                         "Does not have a picking with the expected priority")

    def test09_create_batch_user_already_has_completed_batch(self):
        """
        When dropping off a partially reserved picking, a backorder in state
        confirmed is created and remains in the batch. This backorder should
        be removed from the batch, allowing the batch to be automatically
        completed and the user should be able to create a new batch without
        any problem.

        """
        Batch = self.env['stock.picking.batch']
        Package = self.env['stock.quant.package']
        Batch = Batch.sudo(self.outbound_user)

        # set a batch with a complete picking
        self.create_quant(self.apple.id, self.test_location_01.id, 2,
                          package_id=self.package_one.id)
        # Create a picking partially reserved
        picking = self.create_picking(self.picking_type_pick,
                                      products_info=self.pack_4apples_info,
                                      confirm=True,
                                      assign=True)
        batch = Batch.create_batch(self.picking_type_pick.id, None)
        for ml in picking.move_line_ids:
            ml.qty_done = ml.product_qty
        # On drop off a backorder is created for the remaining 2 units,
        # but _check_batches() removes it from the batch since it is not ready
        batch.drop_off_picked(continue_batch=True,
                              move_line_ids=None,
                              location_barcode=self.test_output_location_01.name,
                              result_package_name=None)

        # check the picking is done and the backorder is not in the batch
        self.assertEqual(len(batch.picking_ids), 1)
        self.assertEqual(batch.state, 'done')
        self.assertEqual(batch.picking_ids[0].state, 'done')

        # create a new picking to be included in the new batch
        other_pack = Package.get_package("test_other_package", create=True)
        self.create_quant(self.apple.id, self.test_location_01.id, 4,
                          package_id=other_pack.id)
        other_picking = self.create_picking(self.picking_type_pick,
                                            products_info=self.pack_4apples_info,
                                            confirm=True,
                                            assign=True)

        new_batch = Batch.create_batch(self.picking_type_pick.id, None)

        # check outcome
        self.assertIsNotNone(new_batch, "No batch created")
        self.assertEqual(len(new_batch.picking_ids), 1,
                         "Multiple pickings were included in the batch")
        self.assertEqual(new_batch.picking_ids[0].id, other_picking.id,
                         "Does not include the expected picking")
        self.assertEqual(batch.state, 'done', "Old batch was not completed")

    def test10_create_batch_error_user_has_incomplete_batched_pickings(self):
        """
        Should error in case a the user already has a batch assigned
        to him with incomplete pickings.

        """
        Batch = self.env['stock.picking.batch']
        Package = self.env['stock.quant.package']
        Batch = Batch.sudo(self.outbound_user)

        # set a batch with a complete picking
        self.create_quant(self.apple.id, self.test_location_01.id, 4,
                          package_id=self.package_one.id)
        self.create_picking(self.picking_type_pick,
                            products_info=self.pack_4apples_info,
                            confirm=True,
                            assign=True)
        batch = Batch.create_batch(self.picking_type_pick.id, None)

        # create a new picking to be included in the new batch
        other_pack = Package.get_package("test_other_package", create=True)
        self.create_quant(self.apple.id, self.test_location_01.id, 4,
                          package_id=other_pack.id)
        self.create_picking(self.picking_type_pick,
                            products_info=self.pack_4apples_info,
                            confirm=True,
                            assign=True)

        # check pre-conditions
        self.assertEqual(len(batch.picking_ids), 1)
        self.assertEqual(batch.state, 'in_progress')
        self.assertEqual(batch.picking_ids[0].state, 'assigned')

        # method under test
        with self.assertRaises(ValidationError) as err:
            Batch.create_batch(self.picking_type_pick.id, None)

        self.assertTrue(
            err.exception.name.startswith("The user already has pickings"))

    def test11_automatic_batch_done(self):
        """ Verifies the batch is done if the picking is complete """
        Batch = self.env['stock.picking.batch']
        Batch = Batch.sudo(self.outbound_user)

        self.create_quant(self.apple.id, self.test_location_01.id, 4,
                          package_id=self.package_one.id)
        picking = self.create_picking(self.picking_type_pick,
                                      products_info=self.pack_4apples_info,
                                      confirm=True,
                                      assign=True)
        batch = Batch.create_batch(self.picking_type_pick.id, None)
        picking.update_picking(force_validate=True,
                               location_dest_id=self.test_output_location_01.id)

        # check pre-conditions
        self.assertEqual(len(batch.picking_ids), 1)
        self.assertEqual(batch.state, 'done')
        self.assertEqual(batch.picking_ids[0].state, 'done')

    def _create_valid_batch(self):
        Batch = self.env['stock.picking.batch']
        Batch = Batch.sudo(self.outbound_user)

        self.create_quant(self.apple.id, self.test_location_01.id, 4,
                          package_id=self.package_one.id)
        picking = self.create_picking(self.picking_type_pick,
                                      products_info=self.pack_4apples_info,
                                      confirm=True,
                                      assign=True)

        return picking, Batch.create_batch(self.picking_type_pick.id, None)

    def test12_is_valid_location_dest_success(self):
        """ Returns True for a valid location """
        picking, batch = self._create_valid_batch()
        picking.update_picking(package_name=self.package_one.name)

        self.assertTrue(
            batch.is_valid_location_dest_id(self.test_output_location_01.id),
            "A valid dest location is wrongly marked as invalid")

    def test13_is_valid_location_dest_failure_invalid_location(self):
        """ Returns False for a invalid location """
        _, batch = self._create_valid_batch()

        self.assertTrue(
            batch.is_valid_location_dest_id(self.test_location_02.id),
            "A valid dest location is wrongly marked as invalid")

    def test14_is_valid_location_dest_failure_unknown_location(self):
        """ Returns False for an unknown location """
        picking, batch = self._create_valid_batch()
        picking.update_picking(package_name=self.package_one.name)

        self.assertFalse(
            batch.is_valid_location_dest_id("this location does not exist"),
            "An invalid dest location is wrongly marked as valid")

    def test15_unpickable_item_single_move_line_success_default_type(self):
        """
        Tests that the picking is confirmed and an stock investigation transfer 
        is created if a picking type is not specified. The picking remains 
        confirmed because there isn't more stock available.
        """
        picking, batch = self._create_valid_batch()
        move_line = picking.move_line_ids[0]
        reason = 'missing item'
        batch.unpickable_item(package_name=move_line.package_id.name,
                              reason=reason)
        unpickable_picking = self.package_one.find_move_lines().picking_id

        self.assertEqual(picking.state, 'confirmed',
                         'picking was not confirmed')
        self.assertEqual(batch.state, 'done',
                         'batch state was not completed')
        self.assertEqual(unpickable_picking.picking_type_id,
                         self.picking_type_internal,
                         'stock investigation picking type not set by unpickable_item')
        self.assertEqual(unpickable_picking.state, 'assigned',
                         'stock investigation picking creation has not completed')

    def test16_unpickable_item_package_not_found(self):
        """
        Tests that a ValidationError is raised if the package cannot be
        found in the system
        """
        Package = self.env['stock.quant.package']
        picking, batch = self._create_valid_batch()

        reason = 'missing item'
        package_name = "NOTAPACKAGENAME666"

        self.assertFalse(Package.search([('name', '=', package_name)]),
                         'Package %s already exists' % package_name)
        expected_error = 'Package not found for identifier %s' % package_name
        with self.assertRaisesRegex(ValidationError, expected_error,
                                    msg='Incorrect error thrown'):
            batch.unpickable_item(package_name=package_name,
                                  reason=reason)

    def test17_unpickable_item_wrong_batch(self):
        """
        Tests that a ValidationError is raised if the package is not on
        the Batch that we requested.
        """
        picking, batch = self._create_valid_batch()
        # Create a quant and picking for a different package
        self.create_quant(self.apple.id, self.test_location_01.id, 4,
                          package_id=self.package_two.id)
        different_picking = self.create_picking(
            self.picking_type_pick,
            products_info=self.pack_4apples_info,
            confirm=True,
            assign=True)
        move_line = different_picking.move_line_ids[0]
        reason = 'missing item'

        expected_error = 'Cannot find move lines todo for unpickable item ' \
                         'in this batch'
        with self.assertRaisesRegex(ValidationError, expected_error,
                                    msg='Incorrect error thrown'):
            batch.unpickable_item(package_name=move_line.package_id.name,
                                  reason=reason)

    def test18_unpickable_item_invalid_state_cancel(self):
        """
        Tests that a ValidationError is raised if the package move lines
        cannot be found in the wave because the picking is on a state of
        cancel
        """
        picking, batch = self._create_valid_batch()
        # Not ideal but it allows the test to pass.  If we did:
        # picking.action_cancel() it would delete the move_lines which would
        # cause this test to fail incorrectly.
        picking.state = 'cancel'
        move_line = picking.move_line_ids[0]
        reason = 'missing item'

        expected_error = 'Cannot find move lines todo for unpickable item ' \
                         'in this batch'
        with self.assertRaisesRegex(ValidationError, expected_error,
                                    msg='Incorrect error thrown'):
            batch.unpickable_item(package_name=move_line.package_id.name,
                                  reason=reason)

    def test19_unpickable_item_invalid_state_done(self):
        """
        Tests that a ValidationError is raised if the package move lines
        cannot be found in the wave because the picking is on a state of
        done
        """
        picking, batch = self._create_valid_batch()
        picking.update_picking(force_validate=True,
                               location_dest_id=self.test_output_location_01.id)

        move_line = picking.move_line_ids[0]
        reason = 'missing item'

        expected_error = 'Cannot find move lines todo for unpickable item ' \
                         'in this batch'
        with self.assertRaisesRegex(ValidationError, expected_error,
                                    msg='Incorrect error thrown'):
            batch.unpickable_item(package_name=move_line.package_id.name,
                                  reason=reason)

    def test20_unpickable_item_multiple_move_lines_different_packages(self):
        """
        Tests that a backorder is created and confirmed if there are multiple
        move lines on the picking. The original picking should continue to
        have the still pickable product on it.
        """
        Batch = self.env['stock.picking.batch']
        Batch = Batch.sudo(self.outbound_user)

        self.create_quant(self.apple.id, self.test_location_01.id, 4,
                          package_id=self.package_one.id)
        self.create_quant(self.banana.id, self.test_location_01.id, 4,
                          package_id=self.package_two.id)
        products_info = [{'product': self.apple,
                          'qty': 4},
                         {'product': self.banana,
                          'qty': 4}]
        picking = self.create_picking(self.picking_type_pick,
                                      products_info=products_info,
                                      confirm=True,
                                      assign=True)
        batch = Batch.create_batch(self.picking_type_pick.id, None)

        self.assertTrue(len(picking.move_line_ids) > 1,
                        'number of move_lines not expected')
        unpickable_move_line = picking.move_line_ids[0]
        unpickable_package = unpickable_move_line.package_id

        reason = 'missing item'

        batch.unpickable_item(package_name=unpickable_package.name,
                              reason=reason)

        new_picking = unpickable_package.find_move_lines().picking_id

        # Because there are other move_line_ids that are still pickable we
        # need to ensure that the original picking is still assigned
        self.assertEqual(picking.state, 'assigned',
                         'picking was not assigned')

        # Check backorder has been created
        self.assertEqual(len(picking.u_created_back_orders), 1)
        # Check backorder state
        self.assertEqual(picking.u_created_back_orders.state, 'confirmed')

        # Ensure that our unpickable move_line is not in the picking
        self.assertNotIn(unpickable_move_line, picking.move_line_ids,
                         'unpickable_move_line has not been removed '
                         'from picking')

        # Ensure investigation picking is assigned and with the reason
        self.assertEqual(new_picking.state, 'assigned',
                         'picking creation has not completed properly')
        self.assertEqual(new_picking.group_id.name, reason,
                         'reason was not assigned correctly')

        # Check one backorder has been created
        self.assertEqual(len(picking.u_created_back_orders), 1)

    def test21_unpickable_item_multiple_move_lines_different_packages_available(self):
        """
        Tests that when the unpickable item is available, a new move line
        is added to the picking.
        """
        Batch = self.env['stock.picking.batch']
        Batch = Batch.sudo(self.outbound_user)

        self.create_quant(self.apple.id, self.test_location_01.id, 4,
                          package_id=self.package_one.id)
        self.create_quant(self.banana.id, self.test_location_01.id, 4,
                          package_id=self.package_two.id)
        self.create_quant(self.apple.id, self.test_location_02.id, 6,
                          package_id=self.package_three.id)
        self.create_quant(self.banana.id, self.test_location_02.id, 7,
                          package_id=self.package_four.id)

        products_info = [{'product': self.apple,
                          'qty': 4},
                         {'product': self.banana,
                          'qty': 4}]
        picking = self.create_picking(self.picking_type_pick,
                                      products_info=products_info,
                                      confirm=True,
                                      assign=True)
        batch = Batch.create_batch(self.picking_type_pick.id, None)
        num_move_lines = len(picking.move_line_ids)

        self.assertTrue(num_move_lines > 1,
                        'number of move_lines not expected')
        unpickable_move_line = picking.move_line_ids[0]
        unpickable_package = unpickable_move_line.package_id
        reason = 'missing item'

        batch.unpickable_item(package_name=unpickable_package.name,
                              reason=reason)

        self.assertEqual(num_move_lines, len(picking.move_line_ids),
                         'Number of move lines changed')
        self.assertEqual(picking.state, 'assigned',
                         'picking was not assigned')

        # Check no backorder has been created
        self.assertEqual(len(picking.u_created_back_orders), 0)

    def test22_unpickable_item_multiple_move_lines_same_package(self):
        """
        Tests that if there are multiple move lines on the same package
        that the picking remains in state confirmed and a new picking
        is created of type picking_types
        """
        self.create_quant(self.apple.id, self.test_location_01.id, 4,
                          package_id=self.package_one.id)
        self.create_quant(self.banana.id, self.test_location_01.id, 4,
                          package_id=self.package_one.id)
        products_info = [{'product': self.apple,
                          'qty': 4},
                         {'product': self.banana,
                          'qty': 4}]
        picking = self.create_picking(self.picking_type_pick,
                                      products_info=products_info,
                                      confirm=True,
                                      assign=True)
        Batch = self.env['stock.picking.batch']
        Batch = Batch.sudo(self.outbound_user)
        batch = Batch.create_batch(self.picking_type_pick.id, None)

        self.assertTrue(len(picking.move_line_ids) > 1,
                        'number of move_lines not expected')
        unpickable_move_line = picking.move_line_ids[0]
        unpickable_package = unpickable_move_line.package_id

        reason = 'missing item'

        batch.unpickable_item(package_name=unpickable_package.name,
                              reason=reason)

        new_picking = unpickable_package.find_move_lines().mapped('picking_id')
        self.assertEqual(picking.state, 'confirmed',
                         'picking was not confirmed')
        # Check no backorder has been created
        self.assertEqual(len(picking.u_created_back_orders), 0)

        self.assertEqual(new_picking.state, 'assigned',
                         'picking creation has not completed properly')
        self.assertEqual(new_picking.group_id.name, reason,
                         'reason was not assigned correctly')

    def test23_unpickable_item_product_validation_error_missing_location(self):
        """
        Tests that calling unpickable item for a product without location
        raises an error.
        """
        quant = self.create_quant(self.apple.id, self.test_location_01.id, 4)

        products_info = [{'product': self.apple, 'qty': 1}]
        picking = self.create_picking(self.picking_type_pick,
                                      products_info=products_info,
                                      confirm=True,
                                      assign=True)
        # check that only 1 unit of the quant is reserved
        self.assertEqual(quant.reserved_quantity, 1)

        Batch = self.env['stock.picking.batch']
        Batch = Batch.sudo(self.outbound_user)
        batch = Batch.create_batch(self.picking_type_pick.id, None)

        self.assertIn(picking, batch.picking_ids)

        reason = 'missing item'
        move_line = picking.move_line_ids[0]

        expected_error = 'Missing location parameter for unpickable' \
                         ' product %s' % move_line.product_id.name
        with self.assertRaisesRegex(ValidationError, expected_error,
                                    msg='Incorrect error thrown'):
            batch.unpickable_item(product_id=move_line.product_id.id,
                                  reason=reason)

    def test24_unpickable_item_product_ok(self):
        """
        Tests that calling unpickable item for a product with location
        ends up with all the quant reserved for the stock investigation
        and the picking remains in state confirmed since there is no
        more stock.
        """
        quant = self.create_quant(self.apple.id, self.test_location_01.id, 4)

        products_info = [{'product': self.apple, 'qty': 1}]
        picking = self.create_picking(self.picking_type_pick,
                                      products_info=products_info,
                                      confirm=True,
                                      assign=True)
        # check that only 1 unit of the quant is reserved
        self.assertEqual(quant.reserved_quantity, 1)

        Batch = self.env['stock.picking.batch']
        Batch = Batch.sudo(self.outbound_user)
        batch = Batch.create_batch(self.picking_type_pick.id, None)

        self.assertIn(picking, batch.picking_ids)

        reason = 'missing item'
        move_line = picking.move_line_ids[0]

        batch.unpickable_item(product_id=move_line.product_id.id,
                              location_id=move_line.location_id.id,
                              reason=reason)
        # after unickable all the quant should be reserved
        self.assertEqual(quant.reserved_quantity, 4)
        # picking state should be confirmed
        self.assertEqual(picking.state, 'confirmed',
                         'picking was not confirmed')
        # Check no backorder has been created
        self.assertEqual(len(picking.u_created_back_orders), 0)

    def test25_unpickable_item_product_ok_multiple_lines(self):
        """
        Tests that calling unpickable item for a product with location
        ends up with all the quant reserved for the stock investigation.
        In this case we have multiple move lines at the picking, so a
        backorder is created and its state is confirmed since there
        is no more stock.
        """
        quant_apple = self.create_quant(self.apple.id, self.test_location_01.id, 4)
        quant_banana = self.create_quant(self.banana.id, self.test_location_01.id, 3)

        products_info = [{'product': self.apple, 'qty': 1},
                         {'product': self.banana, 'qty': 2}]
        picking = self.create_picking(self.picking_type_pick,
                                      products_info=products_info,
                                      confirm=True,
                                      assign=True)
        # check that only 1 unit of the quant is reserved
        self.assertEqual(quant_apple.reserved_quantity, 1)
        self.assertEqual(quant_banana.reserved_quantity, 2)

        Batch = self.env['stock.picking.batch']
        Batch = Batch.sudo(self.outbound_user)
        batch = Batch.create_batch(self.picking_type_pick.id, None)

        self.assertIn(picking, batch.picking_ids)

        reason = 'missing item'
        move_line = picking.move_line_ids[0]

        batch.unpickable_item(product_id=move_line.product_id.id,
                              location_id=move_line.location_id.id,
                              reason=reason)
        # after unickable all the quant should be reserved
        self.assertEqual(quant_apple.reserved_quantity, 4)
        # picking state should be assigned
        self.assertEqual(picking.state, 'assigned',
                         'picking was not assigned')

        # Check one backorder has been created
        self.assertEqual(len(picking.u_created_back_orders), 1)
        # Check backorder state
        self.assertEqual(picking.u_created_back_orders.state, 'confirmed')

    def test26_unpickable_item_product_ok_multiple_lines(self):
        """
        Tests that calling unpickable item for a product with location
        ends up with all the quant reserved for the stock investigation.
        In this case, the unpickable item is available elsewhere, so we
        don't create a backorder.
        """
        quant_apple_1  = self.create_quant(self.apple.id, self.test_location_01.id, 4)
        quant_banana_1 = self.create_quant(self.banana.id, self.test_location_01.id, 3)
        quant_apple_2  = self.create_quant(self.apple.id, self.test_location_02.id, 4)
        quant_banana_2 = self.create_quant(self.banana.id, self.test_location_02.id, 3)

        products_info = [{'product': self.apple, 'qty': 1},
                         {'product': self.banana, 'qty': 2}]
        picking = self.create_picking(self.picking_type_pick,
                                      products_info=products_info,
                                      confirm=True,
                                      assign=True)

        # determine what was the quant the system did reserve
        reserved_quant_apple = None
        unreserved_quant_apple = None
        reserved_quant_banana = None
        unreserved_quant_banana = None

        if quant_apple_1.reserved_quantity == 1:
            reserved_quant_apple = quant_apple_1
            unreserved_quant_apple = quant_apple_2
            self.assertTrue(quant_apple_2.reserved_quantity == 0,
                            'Both apple quants reserved')
        elif quant_apple_2.reserved_quantity == 1:
            reserved_quant_apple = quant_apple_2
            unreserved_quant_apple = quant_apple_1
            self.assertTrue(quant_apple_1.reserved_quantity == 0,
                            'Both apple quants reserved')
        else:
            self.assertTrue(False, 'No apple quant reserved')

        if quant_banana_1.reserved_quantity == 2:
            reserved_quant_banana = quant_banana_1
            unreserved_quant_banana = quant_banana_2
            self.assertTrue(quant_banana_2.reserved_quantity == 0,
                            'Both banana quants reserved')
        elif quant_banana_2.reserved_quantity == 2:
            reserved_quant_banana = quant_banana_2
            unreserved_quant_banana = quant_banana_1
            self.assertTrue(quant_banana_1.reserved_quantity == 0,
                            'Both banana quants reserved')
        else:
            self.assertTrue(False, 'No banana quant reserved')


        Batch = self.env['stock.picking.batch']
        Batch = Batch.sudo(self.outbound_user)
        batch = Batch.create_batch(self.picking_type_pick.id, None)

        self.assertIn(picking, batch.picking_ids)

        reason = 'missing item'
        move_line = picking.move_line_ids[0]

        batch.unpickable_item(product_id=move_line.product_id.id,
                              location_id=move_line.location_id.id,
                              reason=reason)

        # after unpickable all the unpickable apple quant should be reserved
        self.assertTrue(reserved_quant_apple.reserved_quantity == 4,
                        'Not all the apple has been reserved for investingation')
        # and the other apple quant will be used for the picking
        self.assertTrue(unreserved_quant_apple.reserved_quantity == 1,
                        'Not all the apple has been reserved for investingation')
        # whilt the banana quants shouldn't change
        self.assertTrue(reserved_quant_banana.reserved_quantity == 2,
                        'The banana quant unexpectedly changed')
        self.assertTrue(unreserved_quant_banana.reserved_quantity == 0,
                        'The banana quant unexpectedly changed')
        # picking state should be assigned
        self.assertEqual(picking.state, 'assigned',
                         'picking was not assigned')
        # no backorder has been created
        self.assertEqual(len(picking.u_created_back_orders), 0)

    def test27_unpickable_item_product_ok_two_picks(self):
        """
        Tests that calling unpickable item for a product with location
        ends up with all the quant reserved for the stock investigation
        and the picking remains in state confirmed since there is no
        more stock. In case the quant is reserved for more than one picking
        the stock investigation will contain only the quantity of the
        unpickable + available quantity of the quant.
        Example: quant of 4, 1 unit reserved in two pickings, leaves
                 an available quantity of 2, so when unpickable of one
                 of the pickings will create an investigation of 3.
        """
        Picking = self.env['stock.picking']
        Batch = self.env['stock.picking.batch']
        Batch = Batch.sudo(self.outbound_user)

        quant = self.create_quant(self.apple.id, self.test_location_01.id, 4)

        products_info = [{'product': self.apple, 'qty': 1}]
        picking = self.create_picking(self.picking_type_pick,
                                      products_info=products_info,
                                      confirm=True,
                                      assign=True)
        # check that only 1 unit of the quant is reserved
        self.assertEqual(quant.reserved_quantity, 1)

        picking_2 = self.create_picking(self.picking_type_pick,
                                      products_info=products_info,
                                      confirm=True,
                                      assign=True)
        # check that now 2 units of the quant are reserved
        self.assertEqual(quant.reserved_quantity, 2)

        batch = Batch.create_batch(self.picking_type_pick.id, None)

        self.assertIn(picking, batch.picking_ids)

        reason = 'missing item'
        move_line = picking.move_line_ids[0]

        batch.unpickable_item(product_id=move_line.product_id.id,
                              location_id=move_line.location_id.id,
                              reason=reason)
        # after unickable all the quant should be reserved
        self.assertEqual(quant.reserved_quantity, 4)
        # picking state should be confirmed
        self.assertEqual(picking.state, 'confirmed',
                         'picking was not confirmed')
        # Check no backorder has been created
        self.assertEqual(len(picking.u_created_back_orders), 0)
        # check that the investigation has reserved 3 only
        inv_picking = Picking.search([('picking_type_id', '=', self.picking_type_internal.id)])
        self.assertEqual(len(inv_picking), 1)
        self.assertEqual(len(inv_picking.move_line_ids), 1)
        self.assertEqual(inv_picking.move_line_ids[0].product_qty, 3)


class TestPickingBatchDisabledUnpickableItems(common.BaseUDES):

    @classmethod
    def setUpClass(cls):
        super(TestPickingBatchDisabledUnpickableItems, cls).setUpClass()
        cls.pack_4apples_info = [{'product': cls.apple, 'qty': 4}]
        # disable unpickable items by default
        cls.picking_type_pick.u_enable_unpickable_items = False

    def setUp(self):
        super(TestPickingBatchDisabledUnpickableItems, self).setUp()
        Package = self.env['stock.quant.package']

        self.package_one = Package.get_package("test_package_one",
                                               create=True)

    def _create_valid_batch(self):
        Batch = self.env['stock.picking.batch']
        Batch = Batch.sudo(self.outbound_user)

        self.create_quant(self.apple.id, self.test_location_01.id, 4,
                          package_id=self.package_one.id)
        picking = self.create_picking(self.picking_type_pick,
                                      products_info=self.pack_4apples_info,
                                      confirm=True,
                                      assign=True)

        return picking, Batch.create_batch(self.picking_type_pick.id, None)

    def test01_disabled_unpickable_item_single_move_line_success_default_type(self):
        """ Raise an error when unpickable items is not enabled, and
            everything remains the same.
        """
        picking, batch = self._create_valid_batch()
        move_line = picking.move_line_ids[0]
        reason = 'missing item'
        expected_error = 'This type of operation cannot handle unpickable ' \
                         'items. Please, contact your team leader to resolve ' \
                         'the issue. Press back when resolved.'
        with self.assertRaisesRegex(ValidationError, expected_error,
                                    msg='Incorrect error thrown'):
            batch.unpickable_item(package_name=move_line.package_id.name,
                                  reason=reason)

        package_picking = self.package_one.find_move_lines().picking_id

        self.assertEqual(package_picking, picking)


class TestBatchGetNextTask(common.BaseUDES):

    @classmethod
    def setUpClass(cls):
        super(TestBatchGetNextTask, cls).setUpClass()
        cls.pack_2prods_info = [{'product': cls.apple, 'qty': 4},
                                {'product': cls.banana, 'qty': 4}]

    def test01_picking_ordering_is_persisted_in_task(self):
        """ Ensure that get_next_task respects the ordering criteria """
        Package = self.env['stock.quant.package']
        package_a = Package.get_package("1", create=True)
        package_b = Package.get_package("2", create=True)

        self.create_quant(self.apple.id, self.test_location_01.id, 4,
                          package_id=package_a.id)
        self.create_quant(self.banana.id, self.test_location_02.id, 4,
                          package_id=package_b.id)
        picking = self.create_picking(self.picking_type_pick,
                                      products_info=self.pack_2prods_info,
                                      confirm=True,
                                      assign=True)
        batch = self.create_batch(user=self.outbound_user)
        picking.batch_id = batch.id

        criteria = lambda ml: (int(ml.package_id.name))
        task = batch.get_next_task(task_grouping_criteria=criteria)

        # We should get the move line related to package named '1'
        self.assertEqual(task['package_id']['name'], '1')

        task = batch.get_next_task(task_grouping_criteria=criteria)

        # Calling get_next_task again should give the same task
        self.assertEqual(task['package_id']['name'], '1')

        package_a.write({'name': '10'})
        task = batch.get_next_task(task_grouping_criteria=criteria)

        # As package_a is now named '10', the criteria should give the '2'
        self.assertEqual(task['package_id']['name'], '2')

        package_b.write({'name': '12341234'})
        task = batch.get_next_task(task_grouping_criteria=criteria)

        # In the same way, we should now get '10'
        self.assertEqual(task['package_id']['name'], '10')


class TestBatchAddRemoveWork(common.BaseUDES):

    @classmethod
    def setUpClass(cls):
        super(TestBatchAddRemoveWork, cls).setUpClass()

        Batch = cls.env['stock.picking.batch']
        Batch = Batch.sudo(cls.outbound_user)

        cls.pack_info = [{'product': cls.apple, 'qty': 4}]
        cls.multipack_info = [{'product': cls.apple, 'qty': 2},
                              {'product': cls.banana, 'qty': 4}]

        cls.create_quant(cls.apple.id, cls.test_location_01.id, 12)
        cls.create_quant(cls.banana.id, cls.test_location_02.id, 8)

        cls.picking = cls.create_picking(cls.picking_type_pick,
                                         products_info=cls.pack_info,
                                         confirm=True,
                                         assign=True,
                                         name='pickingone')
        cls.picking2 = cls.create_picking(cls.picking_type_pick,
                                          products_info=cls.pack_info,
                                          confirm=True,
                                          assign=True,
                                          name='pickingtwo')
        cls.picking3 = cls.create_picking(cls.picking_type_in,
                                          products_info=cls.multipack_info,
                                          confirm=True,
                                          assign=True,
                                          name='pickingthree')
        cls.picking4 = cls.create_picking(cls.picking_type_pick,
                                          products_info=cls.multipack_info,
                                          confirm=True,
                                          assign=True,
                                          name='pickingfour')

        cls.batch = Batch.create_batch(cls.picking_type_pick.id,
                                       [cls.picking.priority])

    @classmethod
    def complete_pick(cls, picking):
        for move in picking.move_lines:
            move.write({
                'quantity_done': move.product_uom_qty,
                'location_dest_id': cls.test_output_location_01.id,
            })

    def test01_test_add_extra_pickings(self):
        """ Ensure that add extra picking adds pickings correctly"""

        batch = self.batch

        # We only have one picking at the moment
        self.assertEqual(len(batch.picking_ids), 1)
        self.assertEqual(batch.picking_ids[0].name, 'pickingone')

        # Add extra pickings
        batch.add_extra_pickings(self.picking_type_pick.id)

        # Check that we now have two pickings including "pickingtwo"
        self.assertEqual(len(batch.picking_ids), 2)
        self.assertIn('pickingtwo', batch.mapped('picking_ids.name'))

        # Add extra pickings
        batch.add_extra_pickings(self.picking_type_pick.id)

        # Check that we now have three pickings including "pickingfour"
        self.assertEqual(len(batch.picking_ids), 3)
        self.assertIn('pickingfour', batch.mapped('picking_ids.name'))

        # Should be no more work now, check error is raised
        with self.assertRaises(ValidationError) as err:
            batch.add_extra_pickings(self.picking_type_pick.id)

    def test02_test_remove_unfinished_work(self):
        """ Ensure that remove unfinished work removes picks
        and backorders moves correctly """

        picking = self.picking
        picking2 = self.picking2
        picking4 = self.picking4
        batch = self.batch

        pickings = picking2 + picking4
        pickings.write({'batch_id': batch.id})

        # We have three pickings in this batch now
        self.assertEqual(len(batch.picking_ids), 3)

        # Complete pick2
        self.complete_pick(picking2)

        # semi complete pick3
        picking4.move_lines[0].write({
                'quantity_done': 2,
                'location_dest_id': self.test_output_location_01.id,
            })

        # Record which move lines were complete and which weren't
        done_moves = picking4.move_lines[0]+picking2.move_lines[0]
        incomplete_moves = picking.move_lines[0]+picking4.move_lines[1]

        # Remove unfinished work
        batch.remove_unfinished_work()

        # Pickings with incomplete work are removed, complete pickings remain
        self.assertFalse(picking.batch_id)
        self.assertEqual(picking2.batch_id, batch)
        self.assertFalse(picking4.batch_id)

        # Ensure both done moves remain in batch
        self.assertEqual(done_moves.mapped('picking_id.batch_id'), batch)

        # Ensure incomplete moves are in pickings that are not in batches
        self.assertFalse(incomplete_moves.mapped('picking_id.batch_id'))


class TestBatchState(common.BaseUDES):
    @classmethod
    def setUpClass(cls):
        super(TestBatchState, cls).setUpClass()
        cls.env.user.get_user_warehouse().u_remove_unready_batch = True
        cls.env.user.get_user_warehouse().u_auto_assign_batch = True

        Package = cls.env['stock.quant.package']
        cls.package_one = Package.get_package("test_package_one", create=True)
        cls.package_two = Package.get_package("test_package_two", create=True)

        cls.pack_4apples_info = [{'product': cls.apple, 'qty': 4}]

        cls.batch01 = cls.create_batch(user=False)

        cls.picking01 = cls.create_picking(
            cls.picking_type_pick,
            products_info=cls.pack_4apples_info,
            confirm=True,
            batch_id=cls.batch01.id,
        )

        cls.picking02 = cls.create_picking(
            cls.picking_type_pick,
            products_info=cls.pack_4apples_info,
            confirm=True,
        )

        cls.compute_patch = patch.object(
            cls.batch01.__class__, '_compute_state',
            autospec=True
        )


    @classmethod
    def draft_to_ready(cls):
        """
            Setup method for moving 'draft' to 'ready'.
            Note, this assumes picking01 to still have batch01 as batch.
        """
        cls.batch01.confirm_picking()
        cls.create_quant(cls.apple.id, cls.test_location_01.id, 4,
                        package_id=cls.package_one.id)
        cls.picking01.action_assign()

    @classmethod
    def assign_user(cls):
        """Method to attach outbound user to batch"""
        cls.batch01.user_id = cls.outbound_user.id

    @classmethod
    def complete_pick(cls, picking, call_done=True):
        for move in picking.move_lines:
            move.write({
                'quantity_done': move.product_uom_qty,
                'location_dest_id': cls.test_output_location_01.id,
            })
        picking.move_line_ids.write({'location_dest_id': cls.test_output_location_01.id})

        if call_done:
            picking.action_done()

    def test00_empty_simple_flow(self):
        """Create and try to go through the stages"""

        self.assertEqual(self.batch01.state, 'draft')
        self.assertEqual(self.batch01.picking_ids.state, 'confirmed')

        # Move from draft to ready, check batch state ready
        self.draft_to_ready()
        self.assertEqual(self.picking01.state, 'assigned')
        self.assertEqual(self.batch01.state, 'ready')

        # Attach user to ready batch, check that it becomes in progress
        self.assign_user()
        self.assertEqual(self.batch01.state, 'in_progress')

        # Perform moves and action_done, confirm batch and pickings 'done'
        self.complete_pick(self.picking01)
        self.assertEqual(self.picking01.state, 'done')
        self.assertEqual(self.batch01.state, 'done')

    def test01_ready_to_waiting(self):
        """Get to ready then check that we can move back to waiting"""
        self.draft_to_ready()
        # Add another picking to go back!
        self.picking02.batch_id = self.batch01.id
        self.assertEqual(self.batch01.state, 'waiting')

        # Remove picking to go back to ready...
        self.picking02.batch_id = False
        self.assertEqual(self.batch01.state, 'ready')

    def test02_waiting_to_in_progess(self):
        """ Assign user to check we get in_progress, then move back"""
        self.draft_to_ready()
        self.assign_user()
        self.assertEqual(self.batch01.state, 'in_progress')
        # Check that removing user moves back to ready
        self.batch01.user_id = False
        self.assertEqual(self.batch01.state, 'ready')

    def test03_cancel_pick_to_done(self):
        """ Cancel pick and confirm state 'done'"""
        self.draft_to_ready()
        self.assign_user()
        # Cancel the pick and confirm we reach state done
        self.picking01.action_cancel()
        self.assertEqual(self.batch01.state, 'done')

    def test04_potential_assignment(self):
        """ Add picking which is not ready leads to removal from batch"""
        self.draft_to_ready()
        self.assign_user()
        self.picking02.batch_id = self.batch01
        self.assertNotIn(self.picking02, self.batch01.picking_ids)

    def test05_remove_batch_id(self):
        """ Remove batch_id from picking and confirm state 'done'"""
        self.draft_to_ready()
        self.assign_user()
        self.picking01.batch_id = False
        self.assertEqual(self.batch01.state, 'done')

    def test06_ready_picking_to_batch(self):
        """ Add picking in state 'assigned' to 'draft' batch, goes to 'ready'
            on confirm_picking.
        """
        self.create_quant(self.apple.id, self.test_location_01.id, 4,
                          package_id=self.package_one.id)
        self.picking01.action_assign()
        self.batch01.confirm_picking()
        self.assertEqual(self.batch01.state, 'ready')

    def test07_partial_completion(self):
        """ Check state remains in_progress when batch pickings partially
            completed.
        """
        self.draft_to_ready()
        self.assign_user()
        self.assertEqual(self.batch01.state, 'in_progress')

        # Create second quant and assign picking, confirm 'in_progress' state
        self.create_quant(self.apple.id, self.test_location_01.id, 4,
                          package_id=self.package_two.id)
        self.picking02.action_assign()
        self.picking02.batch_id = self.batch01
        self.assertEqual(self.batch01.state, 'in_progress')

        # Move and complete picking01, confirm batch remains 'in_progress'
        self.complete_pick(self.picking01)
        self.assertEqual(self.picking01.state, 'done')
        self.assertEqual(self.batch01.state, 'in_progress')

        # Move and complete picking02, confirm batch 'done' state
        self.complete_pick(self.picking02)
        self.assertEqual(self.batch01.state, 'done')

    def test08_check_computing_simple(self):
        """ Checking that we are going into _compute_state as expected
            i.e. with the right object
        """
        self.assertEqual(self.batch01.state, 'draft')

        # mock compute_state method to allow checking of called value and
        # the batch it was called on.
        with self.compute_patch as mocked_compute:
            self.batch01.confirm_picking()

        mocked_compute.assert_called_with(self.batch01)
        self.assertGreaterEqual(
            mocked_compute.call_count, 1,
            "The function that computes state wasn't invoked"
        )
        self.assertEqual(self.batch01.state, 'waiting')

        self.create_quant(self.apple.id, self.test_location_01.id, 4,
                          package_id=self.package_one.id)

        # use context manager to perform action_assign with the mocked_compute
        # check that it is called as expected (once by the correct object)
        # finally forcibly compute the state as the mocked_version while called
        # by the picking doesn't behave correctly
        with self.compute_patch as mocked_compute:
            self.picking01.action_assign()
            mocked_compute.assert_called_with(self.batch01)
            self.assertGreaterEqual(
                mocked_compute.call_count, 1,
                "The function that computes state wasn't invoked"
            )
        self.batch01._compute_state()
        self.assertEqual(self.batch01.state, 'ready')
        # put in state 'in_progress', can't be tested with patch as decorator
        # constraint won't hit the mocked version of _compute_state
        self.assign_user()
        self.assertEqual(self.batch01.state, 'in_progress')

        # complete picks and check state done, forcibly compute_state
        self.complete_pick(self.picking01, call_done=False)
        with self.compute_patch as mocked_compute:
            self.picking01.action_done()

        mocked_compute.assert_called_with(self.batch01)
        self.assertGreaterEqual(
            mocked_compute.call_count, 1,
            "The function that computes state wasn't invoked"
        )

        self.batch01._compute_state()
        self.assertEqual(self.batch01.state, 'done')

    def test09_check_computing_cancel(self):
        """ Test done with cancel to check computation"""
        self.draft_to_ready()
        self.assign_user()

        # Cancel the pick and confirm we reach state done, compute state
        with self.compute_patch as mocked_compute:
            self.picking01.action_cancel()

        mocked_compute.assert_called_with(self.batch01)
        self.assertGreaterEqual(
            mocked_compute.call_count, 1,
            "The function that computes state wasn't invoked"
        )

        self.batch01._compute_state()
        self.assertEqual(self.batch01.state, 'done')

    def test10_check_computing_cancel(self):
        """ Test done with cancel to check computation"""
        self.draft_to_ready()
        self.assign_user()

        # set batch_id to False and check state 'done', forcibly recompute state
        # and return to previous state
        with self.compute_patch as mocked_compute:
            self.picking01.batch_id = False

        mocked_compute.assert_called_with(self.batch01)
        self.assertGreaterEqual(
            mocked_compute.call_count, 1,
            "The function that computes state wasn't invoked"
        )
        self.batch01._compute_state()
        self.assertEqual(self.batch01.state, 'done')

    def test11_computing_ready_picking_to_batch(self):
        """ Test done with ready picking to check computation"""
        self.create_quant(self.apple.id, self.test_location_01.id, 4,
                          package_id=self.package_one.id)
        # assign picking before adding to batch, check compute state function
        # called when no pick.state change occurs
        with self.compute_patch as mocked_compute:
            self.picking01.action_assign()

        mocked_compute.assert_called_with(self.batch01)
        self.assertGreaterEqual(
            mocked_compute.call_count, 1,
            "The function that computes state wasn't invoked"
        )
        self.batch01._compute_state()
        self.assertEqual(self.batch01.state, 'draft')

        # confirm picking and check compute_state is run
        with self.compute_patch as mocked_compute:
            self.batch01.confirm_picking()

        mocked_compute.assert_called_with(self.batch01)
        self.assertGreaterEqual(
            mocked_compute.call_count, 1,
            "The function that computes state wasn't invoked"
        )
        self.batch01._compute_state()
        self.assertEqual(self.batch01.state, 'ready')

    def test12_computing_partial_assignment(self):
        """ Test done with partially complete pickings to check computation"""
        self.draft_to_ready()
        self.assign_user()

        # Create second quant and assign picking, confirm 'in_progress' state
        self.create_quant(self.apple.id, self.test_location_01.id, 4,
                          package_id=self.package_two.id)

        with self.compute_patch as mocked_compute:
            self.picking02.action_assign()
            self.picking02.batch_id = self.batch01

        mocked_compute.assert_called_with(self.batch01)
        self.assertGreaterEqual(
            mocked_compute.call_count, 1,
            "The function that computes state wasn't invoked"
        )
        self.batch01._compute_state()
        self.assertEqual(self.batch01.state, 'in_progress')

        # complete pick1 and check state 'in_progress', forcibly compute_state
        self.complete_pick(self.picking01, call_done=False)
        with self.compute_patch as mocked_compute:
            self.picking01.action_done()

        mocked_compute.assert_called_with(self.batch01)
        self.assertGreaterEqual(
            mocked_compute.call_count, 1,
            "The function that computes state wasn't invoked"
        )

        self.batch01._compute_state()
        self.assertEqual(self.batch01.state, 'in_progress')

        # complete pick2 and check state 'done', forcibly compute_state
        self.complete_pick(self.picking02, call_done=False)
        with self.compute_patch as mocked_compute:
            self.picking02.action_done()

        mocked_compute.assert_called_with(self.batch01)
        self.assertGreaterEqual(
            mocked_compute.call_count, 1,
            "The function that computes state wasn't invoked"
        )
        self.batch01._compute_state()
        self.assertEqual(self.batch01.state, 'done')


class TestBatchMultiDropOff(common.BaseUDES):

    @classmethod
    def setUpClass(cls):
        super(TestBatchMultiDropOff, cls).setUpClass()
        cls.pack_2prods_info = [{'product': cls.apple, 'qty': 4},
                                {'product': cls.banana, 'qty': 4}]

    @classmethod
    def product_str(cls, product, qty):
        return "{} x {}".format(product.display_name, qty)

    @classmethod
    def complete_pick(cls, picking):
        for line in picking.move_line_ids:
            line.write({
                'qty_done': line.product_uom_qty,
            })

    def test01_next_drop_off_by_products(self):
        """ Test next drop off criterion by products """
        MoveLine = self.env['stock.move.line']
        Batch = self.env['stock.picking.batch']
        Batch = Batch.sudo(self.outbound_user)

        self.picking_type_pick.u_drop_criterion = 'by_products'

        out_location = self.test_output_location_01.barcode

        # Create quants and picking for one order and two products
        self.create_quant(self.apple.id, self.test_location_01.id, 4)
        self.create_quant(self.banana.id, self.test_location_02.id, 4)
        picking = self.create_picking(self.picking_type_pick,
                                      products_info=self.pack_2prods_info,
                                      confirm=True,
                                      assign=True)
        # Create batch for outbound user
        priority = picking.priority
        batch = Batch.create_batch(self.picking_type_pick.id, [priority])

        # Mark all move lines to done
        self.complete_pick(picking)

        # Get next drop off info for apples
        info = batch.get_next_drop_off(self.apple.barcode)
        drop_mls = MoveLine.browse(info['move_line_ids'])
        summary = info['summary']
        last = info['last']

        # Check there are move lines to drop, the summary is for 4 apples and
        # it is not the last drop off (still bananas to drop)
        self.assertTrue(len(drop_mls.exists()) > 0)
        self.assertTrue(self.product_str(self.apple, 4) in summary)
        self.assertFalse(last)

        # Drop off apple move lines, and check expected state of the batch
        batch_after = batch.drop_off_picked(continue_batch=True,
                                            move_line_ids=drop_mls.ids,
                                            location_barcode=out_location,
                                            result_package_name=None)
        self.assertEqual(batch, batch_after)
        self.assertNotEqual(batch.state, 'done')

        # Get next drop off info for apples
        info = batch.get_next_drop_off(self.apple.barcode)
        drop_mls = MoveLine.browse(info['move_line_ids'])
        summary = info['summary']
        last = info['last']

        # Check there are not more apple move lines to drop, the summary is
        # empty and it is not the last drop off (still bananas to drop)
        self.assertEqual(len(drop_mls.exists()), 0)
        self.assertEqual(len(summary), 0)
        self.assertFalse(last)

        # Get next drop off info for bananas
        info = batch.get_next_drop_off(self.banana.barcode)
        drop_mls = MoveLine.browse(info['move_line_ids'])
        summary = info['summary']
        last = info['last']

        # Check there are move lines to drop, the summary is for 4 bananas and
        # it is the last drop off (nothing else to drop)
        self.assertTrue(len(drop_mls.exists()) > 0)
        self.assertTrue(self.product_str(self.banana, 4) in summary)
        self.assertTrue(last)

        # Drop off banana move lines, and check expected state of the batch
        batch_after = batch.drop_off_picked(continue_batch=True,
                                            move_line_ids=drop_mls.ids,
                                            location_barcode=out_location,
                                            result_package_name=None)
        self.assertEqual(batch, batch_after)
        self.assertEqual(batch.state, 'done')

    def test02_next_drop_off_by_orders(self):
        """ Test next drop off criterion by orders """
        MoveLine = self.env['stock.move.line']
        Batch = self.env['stock.picking.batch']
        Batch = Batch.sudo(self.outbound_user)

        self.picking_type_pick.u_drop_criterion = 'by_orders'

        out_location = self.test_output_location_01.barcode
        out_location2 = self.test_output_location_02.barcode

        # Create quants and picking for two orders and two products
        self.create_quant(self.apple.id, self.test_location_01.id, 8)
        self.create_quant(self.banana.id, self.test_location_02.id, 8)
        picking1 = self.create_picking(self.picking_type_pick,
                                      products_info=self.pack_2prods_info,
                                      confirm=True,
                                      assign=True,
                                      origin="test_order_01")
        # Create batch for outbound user
        priority = picking1.priority
        batch = Batch.create_batch(self.picking_type_pick.id, [priority])

        picking2 = self.create_picking(self.picking_type_pick,
                                       products_info=self.pack_2prods_info,
                                       confirm=True,
                                       assign=True,
                                       origin="test_order_02")
        # Add the second pick to the batch
        picking2.batch_id = batch

        # Mark all move lines to done for both pickings
        self.complete_pick(picking1)
        self.complete_pick(picking2)

        # Get next drop off info for picking2
        info = batch.get_next_drop_off(picking2.origin)
        drop_mls = MoveLine.browse(info['move_line_ids'])
        summary = info['summary']
        last = info['last']

        # Check there are move lines to drop, the summary is for 4 apples and 4
        # bananas and it is not the last drop off (still another order to drop)
        self.assertTrue(len(drop_mls.exists()) > 0)
        self.assertTrue(self.product_str(self.apple, 4) in summary)
        self.assertTrue(self.product_str(self.banana, 4) in summary)
        self.assertFalse(last)

        # Drop off picking2 move lines, and check expected state of the batch
        batch_after = batch.drop_off_picked(continue_batch=True,
                                            move_line_ids=drop_mls.ids,
                                            location_barcode=out_location2,
                                            result_package_name=None)
        self.assertEqual(batch, batch_after)
        self.assertNotEqual(batch.state, 'done')
        # Check state of pickings are as expected
        self.assertEqual(picking1.state, 'assigned')
        self.assertEqual(picking2.state, 'done')

        # Get next drop off info for picking2
        info = batch.get_next_drop_off(self.apple.barcode)
        drop_mls = MoveLine.browse(info['move_line_ids'])
        summary = info['summary']
        last = info['last']

        # Check there are not more picking2 move lines to drop, the summary is
        # empty and it is not the last drop off (still another order to drop)
        self.assertEqual(len(drop_mls.exists()), 0)
        self.assertEqual(len(summary), 0)
        self.assertFalse(last)

        # Get next drop off info for picking1
        info = batch.get_next_drop_off(picking1.origin)
        drop_mls = MoveLine.browse(info['move_line_ids'])
        summary = info['summary']
        last = info['last']

        # Check there are move lines to drop, the summary is for 4 apples and 4
        # bananas and it is the last drop off (nothing else to drop)
        self.assertTrue(len(drop_mls.exists()) > 0)
        self.assertTrue(self.product_str(self.apple, 4) in summary)
        self.assertTrue(self.product_str(self.banana, 4) in summary)
        self.assertTrue(last)

        # Drop off banana move lines, and check expected state of the batch
        batch_after = batch.drop_off_picked(continue_batch=True,
                                            move_line_ids=drop_mls.ids,
                                            location_barcode=out_location,
                                            result_package_name=None)
        self.assertEqual(batch, batch_after)
        self.assertEqual(batch.state, 'done')

    def test03_next_drop_off_nothing_to_drop(self):
        """ Test next drop off criterion by products but nothing to drop """
        MoveLine = self.env['stock.move.line']
        Batch = self.env['stock.picking.batch']
        Batch = Batch.sudo(self.outbound_user)

        self.picking_type_pick.u_drop_criterion = 'by_products'

        # Create quants and picking for one order and two products
        self.create_quant(self.apple.id, self.test_location_01.id, 4)
        self.create_quant(self.banana.id, self.test_location_02.id, 4)
        picking = self.create_picking(self.picking_type_pick,
                                      products_info=self.pack_2prods_info,
                                      confirm=True,
                                      assign=True)
        # Create batch for outbound user
        priority = picking.priority
        batch = Batch.create_batch(self.picking_type_pick.id, [priority])

        # Get next drop off info for apples when nothing has been picked
        info = batch.get_next_drop_off(self.apple.barcode)
        drop_mls = MoveLine.browse(info['move_line_ids'])
        summary = info['summary']
        last = info['last']

        # Check there are not any apple move lines to drop, the summary is
        # empty and it is the last drop off
        self.assertEqual(len(drop_mls.exists()), 0)
        self.assertEqual(len(summary), 0)
        self.assertTrue(last)
