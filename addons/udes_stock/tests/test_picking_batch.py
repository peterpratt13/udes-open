# -*- coding: utf-8 -*-

from . import common

from odoo.exceptions import ValidationError
from unittest.mock import patch

class TestGoodsInPickingBatch(common.BaseUDES):

    @classmethod
    def setUpClass(cls):
        super(TestGoodsInPickingBatch, cls).setUpClass()
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
        self.assertEqual(batch.picking_ids[0], picking)
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

    def test16_unpickable_item_single_move_line_pallet_of_packages(self):
        """
        Tests that the picking is confirmed and an stock investigation transfer
        is created correctly when a pallet of packages is unpickable. The picking remains
        confirmed because there isn't more stock available.
        """
        pallet = self.create_package()
        self.package_one.package_id = pallet.id
        picking, batch = self._create_valid_batch()
        move_line = picking.move_line_ids[0]
        reason = 'missing item'
        batch.unpickable_item(package_name=pallet.name,
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

    def test17_unpickable_item_package_not_found(self):
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

    def test18_unpickable_item_wrong_batch(self):
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

    def test19_unpickable_item_invalid_state_cancel(self):
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

    def test20_unpickable_item_invalid_state_done(self):
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

    def test21_unpickable_item_multiple_move_lines_different_packages(self):
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

    def test22_unpickable_item_multiple_move_lines_different_packages_available(self):
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

    def test23_unpickable_item_multiple_move_lines_same_package(self):
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

    def test24_unpickable_item_product_validation_error_missing_location(self):
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

    def test25_unpickable_item_product_ok(self):
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

    def test26_unpickable_item_product_ok_multiple_lines(self):
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

    def test27_unpickable_item_product_ok_multiple_lines(self):
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
                        'Not all the apple has been reserved for investigation')
        # and the other apple quant will be used for the picking
        self.assertTrue(unreserved_quant_apple.reserved_quantity == 1,
                        'Not all the apple has been reserved for investigation')
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

    def test28_unpickable_item_product_ok_two_picks(self):
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

        self.create_picking(self.picking_type_pick,
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
        # after unpickable all the quant should be reserved
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

    def test29_unpickable_item_product_ok_in_two_picks(self):
        """
        Tests that calling unpickable item for a product with location
        ends up with all the quant reserved for the stock investigation
        and the picking remains in state confirmed since there is no
        more stock. In case the quant is reserved for more than one picking
        the stock investigation will contain only the quantity of the
        unpickable + available quantity of the quant.
        Example: quant of 4, 1 unit reserved in two pickings, both of which are
                 in the batch, leaves an available quantity of 2, so when
                 unpickable of one of the pickings will create an investigation of 4.
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

        picking_2 = self.create_picking(self.picking_type_pick,
                                        products_info=products_info,
                                        confirm=True,
                                        assign=True)
        pickings = picking | picking_2
        # check that now 2 units of the quant are reserved
        self.assertEqual(quant.reserved_quantity, 2)

        batch = Batch.create_batch(self.picking_type_pick.id, None)

        batch.add_extra_pickings(self.picking_type_pick.id)
        self.assertEqual(pickings, batch.picking_ids)

        reason = 'missing item'
        move_line = picking.move_line_ids[0]

        batch.unpickable_item(product_id=move_line.product_id.id,
                              location_id=move_line.location_id.id,
                              reason=reason)
        # after unpickable all the quant should be reserved
        self.assertEqual(quant.reserved_quantity, 4)
        # picking state should be confirmed
        self.assertEqual(pickings.mapped('state'), ['confirmed', 'confirmed'])
        # Check no backorder has been created
        self.assertEqual(len(pickings.filtered('u_created_back_orders')), 0)
        # check that the investigation has reserved 4
        inv_picking = Picking.search([('picking_type_id', '=', self.picking_type_internal.id)])
        self.assertEqual(len(inv_picking), 1)
        self.assertEqual(len(inv_picking.move_line_ids), 1)
        self.assertEqual(inv_picking.move_line_ids.product_qty, 4)

    def test30_unpickable_item_product_ok_plus_two_picks(self):
        """
        Tests that calling unpickable item for a product with location
        ends up with all the quant reserved for the stock investigation
        and the picking remains in state confirmed since there is no
        more stock. In case the quant is reserved for more than one picking
        the stock investigation will contain only the quantity of the
        unpickable + available quantity of the quant.
        Example: quant of 4, 1 unit reserved in two batch pickings, plus
                 another picking leaves an available quantity of 2, so
                 when unpickable of one of the pickings will create an
                 investigation of 3.
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

        picking_2 = self.create_picking(self.picking_type_pick,
                                        products_info=products_info,
                                        confirm=True,
                                        assign=True)
        pickings = picking | picking_2
        batch = Batch.create_batch(self.picking_type_pick.id, None)
        batch.add_extra_pickings(self.picking_type_pick.id)
        self.assertEqual(pickings, batch.picking_ids)

        self.create_picking(self.picking_type_pick,
                            products_info=products_info,
                            confirm=True,
                            assign=True)
        # check that now 3 units of the quant are reserved
        self.assertEqual(quant.reserved_quantity, 3)

        reason = 'missing item'
        move_line = picking.move_line_ids[0]

        batch.unpickable_item(product_id=move_line.product_id.id,
                              location_id=move_line.location_id.id,
                              reason=reason)
        # after unpickable all the quant should be reserved
        self.assertEqual(quant.reserved_quantity, 4)
        # picking state should be confirmed
        self.assertEqual(pickings.mapped('state'), ['confirmed', 'confirmed'])
        # Check no backorder has been created
        self.assertEqual(len(pickings.filtered('u_created_back_orders')), 0)
        # check that the investigation has reserved 3 only
        inv_picking = Picking.search([('picking_type_id', '=', self.picking_type_internal.id)])
        self.assertEqual(len(inv_picking), 1)
        self.assertEqual(len(inv_picking.move_line_ids), 1)
        self.assertEqual(inv_picking.move_line_ids[0].product_qty, 3)

    def test31_backorder_generated_unpickable_item_product_ok_in_two_picks_and_two_products(self):
        """
        Tests that calling unpickable item with a product in two pickings, one
        of which also includes a line for another product, preserves the move
        for the pickable product.
        """
        Picking = self.env['stock.picking']
        Batch = self.env['stock.picking.batch']
        Batch = Batch.sudo(self.outbound_user)

        quant = self.create_quant(self.apple.id, self.test_location_01.id, 4)
        mixed_quant = self.create_quant(self.banana.id, self.test_location_01.id, 4)

        products_info = [{'product': self.apple, 'qty': 1}]
        picking = self.create_picking(self.picking_type_pick,
                                      products_info=products_info,
                                      confirm=True,
                                      assign=True)

        mixed_products_info = [{'product': self.apple, 'qty': 1},
                               {'product': self.banana, 'qty': 1}]
        mixed_picking = self.create_picking(self.picking_type_pick,
                                            products_info=mixed_products_info,
                                            confirm=True,
                                            assign=True)
        pickings = picking | mixed_picking
        # check that now 2 units of the quant are reserved
        self.assertEqual(quant.reserved_quantity, 2)
        # and one of the pickable product
        self.assertEqual(mixed_quant.reserved_quantity, 1)

        batch = Batch.create_batch(self.picking_type_pick.id, None)

        batch.add_extra_pickings(self.picking_type_pick.id)
        self.assertEqual(pickings, batch.picking_ids)

        reason = 'missing item'
        move_line = picking.move_line_ids[0]

        batch.unpickable_item(product_id=move_line.product_id.id,
                              location_id=move_line.location_id.id,
                              reason=reason)
        # after unpickable all the quant should be reserved
        self.assertEqual(quant.reserved_quantity, 4)
        # Check batch contains banana picking
        self.assertEqual(len(batch.picking_ids), 1)
        self.assertEqual(batch.picking_ids.mapped('product_id'), self.banana)
        # Check original picking states
        self.assertEqual(pickings.mapped('state'), ['confirmed', 'assigned'])
        # Check banana picking is assigned
        self.assertEqual(batch.picking_ids.mapped('state'), ['assigned'])
        # Check backorder has been created
        self.assertEqual(len(pickings.filtered('u_created_back_orders')), 1)
        # check that the investigation has reserved 4
        inv_picking = Picking.search([('picking_type_id', '=', self.picking_type_internal.id)])
        self.assertEqual(len(inv_picking), 1)
        self.assertEqual(len(inv_picking.move_line_ids), 1)
        self.assertEqual(inv_picking.move_line_ids.product_qty, 4)

    def _assert_apple_banana_pickings(
        self, pickings, num_pickings, state, move_list_qty, package_list=None
    ):
        """
        Helper to avoid repeated asserts
        Check the number of pickings, state and move list qty of the pickings provided
        If the state is assigned also check the move lines
        If the package_list is provided and the picking is assigned, check the packages
        This is based on the symmetry of the test, that the pickings are
        mirrored and differ only by product and packages
        """
        self.assertEqual(len(pickings), num_pickings)
        self.assertEqual(pickings.mapped("state"), num_pickings * [state])
        all_mvs = pickings.mapped("move_lines")
        apple_mvs = all_mvs.filtered(lambda mv: mv.product_id == self.apple)
        banana_mvs = all_mvs.filtered(lambda mv: mv.product_id == self.banana)
        for mvs in [apple_mvs, banana_mvs]:
            with self.subTest(mvs=mvs):
                self.assertCountEqual(mvs.mapped("product_qty"), move_list_qty)
                if state == "assigned":
                    # If the state is assigned then also check the move lines
                    mls = mvs.mapped("move_line_ids")
                    self.assertCountEqual(mls.mapped("product_uom_qty"), move_list_qty)
        if state == "assigned" and package_list:
            all_mls = all_mvs.mapped("move_line_ids")
            self.assertCountEqual([ml.package_id for ml in all_mls], package_list)

    def test32_correctly_grouped_pickings_with_all_unpickable_items(self):
        """
        Tests that when flagging items in a batch as unpickable, stock investigations
        are correctly raised with the backordered pickings correctly grouped,
        if all the items are unpickable then we return to the original state of the system
        """
        Picking = self.env["stock.picking"]
        Batch = self.env["stock.picking.batch"]
        Group = self.env["procurement.group"]
        # Cron job that deletes empty draft pickings
        unlink_cron = self.env.ref("udes_stock.unlink_empty_pickings_action")
        unlink_cron.active = False

        # Create a list of 4 packages used for the quants of each product.
        # There should be 4 quants of apples with quantities 1,2,3,4 (total 10) with
        # different packages respectively, same for bananas.
        # Create a separate picking for each product for all of the 10 items.
        apple_packages = [self.create_package() for i in range(4)]
        banana_packages = [self.create_package() for i in range(4)]
        for qty, (apple_package, banana_package) in enumerate(
            zip(apple_packages, banana_packages), start=1
        ):
            self.create_quant(
                self.apple.id, self.test_location_01.id, qty, package_id=apple_package.id
            )
            self.create_quant(
                self.banana.id, self.test_location_01.id, qty, package_id=banana_package.id
            )
        apple_picking = self.create_picking(
            self.picking_type_pick,
            products_info=[{"product": self.apple, "qty": 10}],
            confirm=True,
            assign=True,
        )
        banana_picking = self.create_picking(
            self.picking_type_pick,
            products_info=[{"product": self.banana, "qty": 10}],
            confirm=True,
            assign=True,
        )
        pickings = apple_picking | banana_picking

        # To avoid the original pickings getting the group set the grouping configuration
        # on pick picking type after the pickings are created, so they have no associated group.
        self.picking_type_pick.write(
            {"u_post_confirm_action": "group_by_move_key", "u_move_key_format": "{product_id.id}"}
        )
        group_count = Group.search_count([])
        self.assertEqual(group_count, 0)
        self.assertFalse(apple_picking.group_id)
        self.assertFalse(banana_picking.group_id)

        # Create a batch for both pickings, flag three of the four items from
        # each line as unpickable for different reasons and raise stock investigations
        batch = Batch._create_batch(
            self.outbound_user.id, self.picking_type_pick, picking_id=apple_picking.id
        )
        batch.add_extra_pickings(self.picking_type_pick.id)
        self.assertEqual(apple_picking.batch_id, batch)
        self.assertEqual(banana_picking.batch_id, batch)
        self.assertTrue(batch.state, "in progress")
        for reason, package in zip(
            2 * ["damaged", "missing item", "missing item"],
            apple_packages[:-1] + banana_packages[:-1],
        ):
            batch.unpickable_item(
                reason=reason, package_name=package.name, raise_stock_investigation=True
            )
        self.assertEqual(batch.state, "in_progress")

        # Check the state of the original pickings, should both have one move line of qty 4
        self._assert_apple_banana_pickings(
            pickings, 2, "assigned", [4], package_list=apple_packages[3] + banana_packages[3]
        )
        # Check the state of the investigation pickings - should not be grouped
        inv_pickings = Picking.search([("picking_type_id", "=", self.picking_type_internal.id)])
        self._assert_apple_banana_pickings(
            inv_pickings,
            6,
            "assigned",
            [1, 2, 3],
            package_list=apple_packages[:-1] + banana_packages[:-1],
        )

        # Check the state of the pick pickings
        # Expect to have some draft pickings due to refactoring - which are deleted via cron
        # Two confirmed pickings which have everything kicked out but grouped by product
        # Two assigned pickings, which are the original ones
        pick_pickings = Picking.search([("picking_type_id", "=", self.picking_type_pick.id)])
        draft_pickings = pick_pickings.filtered(lambda p: p.state == "draft")
        self.assertGreater(len(draft_pickings), 0)
        unlink_cron.method_direct_trigger()
        pick_pickings = Picking.search([("picking_type_id", "=", self.picking_type_pick.id)])
        self.assertEqual(len(pick_pickings), 4)
        confirmed_pickings = pick_pickings.filtered(lambda p: p.state == "confirmed")
        self._assert_apple_banana_pickings(confirmed_pickings, 2, "confirmed", [1, 2, 3])
        for p in confirmed_pickings:
            with self.subTest(p=p):
                self.assertTrue(p.backorder_id)
                self.assertTrue(p.group_id)
        remaining_pickings = pick_pickings - confirmed_pickings - draft_pickings
        self.assertEqual(remaining_pickings, pickings)

        # Flag the last item of each product as unpickable which should close the batch and
        # result in a single picking for each product
        for package in apple_packages[3] + banana_packages[3]:
            batch.unpickable_item(
                reason="damaged", package_name=package.name, raise_stock_investigation=True
            )
        self.assertEqual(batch.state, "done")
        # Check the state of the investigation pickings - should not be grouped
        inv_pickings = Picking.search([("picking_type_id", "=", self.picking_type_internal.id)])
        self._assert_apple_banana_pickings(
            inv_pickings, 8, "assigned", [1, 2, 3, 4], package_list=apple_packages + banana_packages
        )
        # Check the pickings are correct - two separate ones grouped by product, it merges
        # everything into the created picking with the group.
        # Trigger the cron job to clean up the draft pickings, and remove the backorder id
        unlink_cron.method_direct_trigger()
        pick_pickings = Picking.search([("picking_type_id", "=", self.picking_type_pick.id)])
        self._assert_apple_banana_pickings(pick_pickings, 2, "confirmed", [1, 2, 3, 4])
        for p in pick_pickings:
            with self.subTest(p=p):
                self.assertFalse(p.backorder_id)
                self.assertTrue(p.group_id)


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
        Package = cls.env['stock.quant.package']

        quant_quantity = 4

        cls.package_a = Package.get_package("1", create=True)
        cls.package_b = Package.get_package("2", create=True)
        cls.apple_quant = cls.create_quant(
            cls.apple.id,
            cls.test_location_01.id,
            quant_quantity,
            package_id=cls.package_a.id
        )
        cls.banana_quant = cls.create_quant(
            cls.banana.id,
            cls.test_location_02.id,
            quant_quantity,
            package_id=cls.package_b.id
        )
        pack_2prods_info = [{'product': cls.apple, 'qty': quant_quantity},
                            {'product': cls.banana, 'qty': quant_quantity}]
        cls.picking = cls.create_picking(
            cls.picking_type_pick,
            products_info=pack_2prods_info,
            confirm=True,
            assign=True
        )
        cls.batch = cls.create_batch(user=cls.outbound_user)
        cls.picking.batch_id = cls.batch.id

    def test01_picking_ordering_is_persisted_in_task(self):
        """ Ensure that get_next_task respects the ordering criteria """
        criteria = lambda ml: (int(ml.package_id.name))
        task = self.batch.get_next_task(task_grouping_criteria=criteria)

        # We should get the move line related to package named '1'
        self.assertEqual(task['package_id']['name'], '1')

        task = self.batch.get_next_task(task_grouping_criteria=criteria)

        # Calling get_next_task again should give the same task
        self.assertEqual(task['package_id']['name'], '1')

        self.package_a.write({'name': '10'})
        task = self.batch.get_next_task(task_grouping_criteria=criteria)

        # As package_a is now named '10', the criteria should give the '2'
        self.assertEqual(task['package_id']['name'], '2')

        self.package_b.write({'name': '12341234'})
        task = self.batch.get_next_task(task_grouping_criteria=criteria)

        # In the same way, we should now get '10'
        self.assertEqual(task['package_id']['name'], '10')

    def test02_get_next_tasks(self):
        """Test that get_next_tasks returns multiple remaining tasks"""
        criteria = lambda ml: (int(ml.package_id.name))
        tasks = self.batch.get_next_tasks(
            task_grouping_criteria=criteria,
            limit=100
        )
        self.assertEqual(len(tasks), 2)
        # The first task should be for package '1'
        self.assertEqual(tasks[0]['package_id']['name'], '1')
        # The second task should be for package '2'
        self.assertEqual(tasks[1]['package_id']['name'], '2')

    def test03_skip_products(self):
        """Test that get_next_task respects skipped products"""
        # Assert that the specified product is not in the next task
        for product_id in [self.apple.id, self.banana.id]:
            task = self.batch.get_next_task(skipped_product_ids=[product_id])
            task_quants = task['package_id']['quant_ids']
            task_products = [quant["product_id"]["id"] for quant in task_quants]
            self.assertNotIn(product_id, task_products)

    def test04_skip_products_return(self):
        """Test that get_next_task can return to skipped products
        if u_return_to_skipped is True.
        """
        # First assert if we skip everything with default config, the returned task is empty
        task = self.batch.get_next_task(skipped_product_ids=[self.apple.id, self.banana.id])
        self.assertFalse(task["move_line_ids"])
        self.assertFalse(task.get("package_id"))
        # Change config to allow returning to skipped tasks
        self.picking_type_pick.u_return_to_skipped = True
        # First returned task should include the first skipped product
        for product_ids in [[self.apple.id, self.banana.id], [self.banana.id, self.apple.id]]:
            task = self.batch.get_next_task(skipped_product_ids=product_ids)
            task_quants = task['package_id']['quant_ids']
            task_products = [quant["product_id"]["id"] for quant in task_quants]
            self.assertIn(product_ids[0], task_products)
            self.assertNotIn(product_ids[1], task_products)

    def test05_skip_move_lines(self):
        """Test that get_next_task respects skipped products"""
        move_line_ids = self.batch.picking_ids.mapped("move_line_ids.id")
        # Assert that the specified move_line_id is not in the next task
        for move_line_id in move_line_ids:
            task = self.batch.get_next_task(skipped_move_line_ids=[move_line_id])
            self.assertNotIn(move_line_id, task['move_line_ids'])

    def test06_skip_move_lines_return(self):
        """Test that get_next_task can return to skipped products
        if u_return_to_skipped is True.
        """
        move_line_ids = self.batch.picking_ids.mapped("move_line_ids.id")
        # First assert if we skip everything with default config, the returned task is empty
        task = self.batch.get_next_task(skipped_move_line_ids=move_line_ids)
        self.assertFalse(task["move_line_ids"])
        self.assertFalse(task.get("package_id"))
        # Change config to allow returning to skipped tasks
        self.picking_type_pick.u_return_to_skipped = True
        # First returned task should include the first skipped move_line_id
        for skipped_move_line_ids in [move_line_ids, [ml for ml in reversed(move_line_ids)]]:
            task = self.batch.get_next_task(skipped_move_line_ids=skipped_move_line_ids)
            self.assertIn(skipped_move_line_ids[0], task['move_line_ids'])
            self.assertNotIn(skipped_move_line_ids[1], task['move_line_ids'])

    def test07_get_completed_tasks(self):
        """Test that we can retrieve a list of completed tasks"""
        move_lines = self.batch.picking_ids.mapped("move_line_ids")
        # First check we dont get any tasks when no tasks have been completed
        tasks = self.batch.get_completed_tasks()
        self.assertFalse(tasks)

        # Set move lines to complete
        for ml in move_lines:
            ml.qty_done = ml.product_uom_qty
        tasks = self.batch.get_completed_tasks()
        # Check that returned tasks have the move lines in them
        self.assertEqual(len(tasks), 2)
        completed_move_line_ids = []
        for task in tasks:
            completed_move_line_ids += task["move_line_ids"]
        self.assertCountEqual(completed_move_line_ids, move_lines.mapped("id"))

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


class TestContinuationBatchProcessing(common.BaseUDES):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.pack_4apples_info = [{'product': cls.apple, 'qty': 4}]

    def test01_preserves_user_id_on_closed_batch(self):
        batch = self.create_batch(user=self.outbound_user, u_ephemeral=False)
        batch = batch.sudo(self.outbound_user)
        batch.close()
        self.assertEqual(batch.user_id, self.outbound_user)

    def test02_moves_outstanding_pickings_to_continuation_batch(self):
        self.create_quant(self.apple.id, self.test_location_01.id, 4,)

        batch = self.create_batch(user=self.outbound_user)
        picking = self.create_picking(self.picking_type_pick,
                                      products_info=self.pack_4apples_info,
                                      confirm=True,
                                      assign=True,
                                      batch_id=batch.id)
        batch.state = 'in_progress'
        batch.close()
        self.assertNotEqual(picking.batch_id, batch)

    def test03_adds_sequence_to_original_batch_name(self):
        self.create_quant(self.apple.id, self.test_location_01.id, 4,)

        batch = self.create_batch(user=self.outbound_user)
        picking = self.create_picking(self.picking_type_pick,
                                      products_info=self.pack_4apples_info,
                                      confirm=True,
                                      assign=True,
                                      batch_id=batch.id)
        batch.state = 'in_progress'
        batch.close()
        self.assertRegex(picking.batch_id.name, r'BATCH/\d+-01')

    def test04_increments_sequence_for_continuation_batch(self):
        self.create_quant(self.apple.id, self.test_location_01.id, 4,)

        batch01 = self.create_batch(user=self.outbound_user)
        picking = self.create_picking(self.picking_type_pick,
                                      products_info=self.pack_4apples_info,
                                      confirm=True,
                                      assign=True,
                                      batch_id=batch01.id)
        batch01.state = 'in_progress'
        batch01.close()
        batch02 = picking.batch_id
        batch02.close()
        self.assertRegex(picking.batch_id.name, r'BATCH/\d+-02')

    def test05_sets_original_name(self):
        self.create_quant(self.apple.id, self.test_location_01.id, 4,)

        batch = self.create_batch(user=self.outbound_user)
        picking = self.create_picking(self.picking_type_pick,
                                      products_info=self.pack_4apples_info,
                                      confirm=True,
                                      assign=True,
                                      batch_id=batch.id)
        batch.state = 'in_progress'
        batch.close()
        self.assertEqual(picking.batch_id.u_original_name, batch.name)

class TestPalletReservation(common.BaseUDES):
    def test01_conflicting_reservation(self):
        batch1 = self.create_batch(user=self.outbound_user)
        batch1.state = 'in_progress'
        batch1.reserve_pallet('UDES11111')

        batch2 = self.create_batch(user=self.outbound_user)
        batch2.state = 'in_progress'
        expected_error = 'This pallet is already being used for batch %s.' % \
                         batch1.name
        with self.assertRaisesRegex(ValidationError, expected_error,
                                    msg='Incorrect error thrown'):
            batch2.reserve_pallet('UDES11111')

    def test02_reservation_expiry_when_done(self):
        batch1 = self.create_batch(user=self.outbound_user)
        batch1.state = 'in_progress'
        batch1.reserve_pallet('UDES11111')
        batch1.state = 'done'

        batch2 = self.create_batch(user=self.outbound_user)
        batch2.state = 'in_progress'
        batch2.reserve_pallet('UDES11111')

    def test03_reservation_switch(self):
        batch1 = self.create_batch(user=self.outbound_user)
        batch1.state = 'in_progress'
        batch1.reserve_pallet('UDES11111')
        batch1.reserve_pallet('UDES11112')

        batch2 = self.create_batch(user=self.outbound_user)
        batch2.state = 'in_progress'
        batch2.reserve_pallet('UDES11111')
        expected_error = 'This pallet is already being used for batch %s.' % \
                         batch1.name
        with self.assertRaisesRegex(ValidationError, expected_error,
                                    msg='Incorrect error thrown'):
            batch2.reserve_pallet('UDES11112')

    def test04_rereservation_for_same_batch(self):
        batch = self.create_batch(user=self.outbound_user)
        batch.state = 'in_progress'

        batch.reserve_pallet('UDES11111')
        batch.reserve_pallet('UDES11111')


class TestMultipleOrders(common.BaseUDES):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.pack_4apples_info = [{'product': cls.apple,
                                  'qty': 4}]
        cls.pack_4bananas_info = [{'product': cls.banana,
                                  'qty': 4}]

        cls.create_quant(cls.apple.id,
                         cls.test_location_01.id,
                         300,
                         package_id=cls.create_package().id)
        cls.create_quant(cls.banana.id,
                         cls.test_location_02.id,
                         300,
                         package_id=cls.create_package().id)

        cls.picking_type_pick.u_reserve_pallet_per_picking = True

    def create_batch(self, user=None, **kwargs):
        return super().create_batch(user=user, u_ephemeral=True, **kwargs)

    def create_batch_with_picking(self):
        batch = self.create_batch(user=self.outbound_user)
        self.create_picking(self.picking_type_pick,
                            products_info=self.pack_4apples_info,
                            confirm=True,
                            assign=True,
                            batch_id=batch.id)
        batch.mark_as_todo()
        return batch

    def complete_pick(self, picking):
        for move in picking.move_lines:
            move.write({
                'quantity_done': move.product_uom_qty,
                'location_dest_id': self.test_output_location_01.id,
            })

    def test01_associates_pallet_with_initial_batch_picking(self):
        batch = self.create_batch_with_picking()
        picking = batch.picking_ids
        batch.reserve_pallet('UDES11111', picking=picking)

        self.assertEqual(picking.u_reserved_pallet, 'UDES11111')

    def test02_associates_pallets_with_respective_pickings(self):
        batch = self.create_batch_with_picking()
        picking1 = batch.picking_ids
        batch.reserve_pallet('UDES11111', picking=picking1)
        picking2 = self.create_picking(self.picking_type_pick,
                                       products_info=self.pack_4bananas_info,
                                       confirm=True,
                                       assign=True)
        batch.add_extra_pickings(self.picking_type_pick.id)
        batch.reserve_pallet('UDES11112', picking=picking2)

        self.assertEqual(sorted(batch.mapped('picking_ids.u_reserved_pallet')), ['UDES11111', 'UDES11112'])

    def test03_raises_error_on_reservation_conflict_different_batch(self):
        batch1 = self.create_batch_with_picking()
        picking1 = batch1.picking_ids
        batch1.reserve_pallet('UDES11111', picking=picking1)
        picking2 = self.create_picking(self.picking_type_pick,
                                       products_info=self.pack_4bananas_info,
                                       confirm=True,
                                       assign=True)
        batch1.add_extra_pickings(self.picking_type_pick.id)
        batch1.reserve_pallet('UDES11112', picking=picking2)
        batch2 = self.create_batch_with_picking()
        picking3 = batch2.picking_ids

        expected_error = 'This pallet is already being used for picking %s.' % \
                         picking1.name

        with self.assertRaisesRegex(ValidationError, expected_error,
                                    msg='Incorrect error thrown'):
            batch2.reserve_pallet('UDES11111', picking=picking3)

    def test04_raises_error_on_reservation_conflict_same_batch(self):
        batch = self.create_batch_with_picking()
        picking1 = batch.picking_ids
        batch.reserve_pallet('UDES11111', picking=picking1)

        picking2 = self.create_picking(self.picking_type_pick,
                                       products_info=self.pack_4bananas_info,
                                       confirm=True,
                                       assign=True)
        batch.add_extra_pickings(self.picking_type_pick.id)
        expected_error = 'This pallet is already being used for picking %s.' % \
                         picking1.name

        with self.assertRaisesRegex(ValidationError, expected_error,
                                    msg='Incorrect error thrown'):
            batch.reserve_pallet('UDES11111', picking=picking2)

    def test05_raises_exception_if_picking_is_not_in_batch(self):
        batch = self.create_batch_with_picking()
        picking1 = batch.picking_ids
        batch.reserve_pallet('UDES11111', picking=picking1)
        picking2 = self.create_picking(self.picking_type_pick,
                                       products_info=self.pack_4bananas_info,
                                       confirm=True,
                                       assign=True)
        expected_error = 'Picking %s is not in batch %s.' % \
                         (picking2.name, batch.name)

        with self.assertRaisesRegex(ValidationError, expected_error,
                                    msg='Incorrect error thrown'):
            batch.reserve_pallet('UDES11112', picking=picking2)

    def test06_raises_exception_if_no_picking_provided(self):
        batch = self.create_batch_with_picking()
        expected_error = 'A picking must be specified if pallets are reserved per picking.'

        with self.assertRaisesRegex(ValidationError, expected_error,
                                    msg='Incorrect error thrown'):
            batch.reserve_pallet('UDES11111')

    def test07_clears_reserved_pallet_on_dropoff(self):
        Batch = self.env['stock.picking.batch']
        Batch = Batch.sudo(self.outbound_user)

        batch = self.create_batch_with_picking()
        picking = batch.picking_ids
        batch.reserve_pallet('UDES11111', picking=picking)
        self.complete_pick(picking)

        self.assertEqual(sorted(batch.mapped('picking_ids.u_reserved_pallet')), ['UDES11111'])
        batch.drop_off_picked(continue_batch=False,
                              move_line_ids=picking.move_line_ids.ids,
                              location_barcode=self.test_output_location_01.name, result_package_name=None)

        self.assertEqual(sorted(batch.mapped('picking_ids.u_reserved_pallet')), [False])

    def test08_raises_exception_if_maximum_reservable_pallets_exceeded(self):
        self.picking_type_pick.u_max_reservable_pallets = 1

        batch = self.create_batch_with_picking()
        picking = batch.picking_ids
        batch.reserve_pallet('UDES11111', picking=picking)

        self.create_picking(self.picking_type_pick,
                            products_info=self.pack_4bananas_info,
                            confirm=True,
                            assign=True)
        expected_error = 'Only %d pallets may be reserved at a time.' % self.picking_type_pick.u_max_reservable_pallets

        with self.assertRaisesRegex(ValidationError, expected_error,
                                    msg='Incorrect error thrown'):
            batch.add_extra_pickings(self.picking_type_pick.id)

    def test09_raises_exception_if_batch_created_with_more_than_max_reservable_pallets(self):
        Batch = self.env['stock.picking.batch']

        self.picking_type_pick.u_max_reservable_pallets = 1

        pickings = self.create_picking(self.picking_type_pick,
                                       products_info=self.pack_4apples_info,
                                       confirm=True,
                                       assign=True)
        pickings |= self.create_picking(self.picking_type_pick,
                                        products_info=self.pack_4bananas_info,
                                        confirm=True,
                                        assign=True)
        expected_error = 'Only %d pallets may be reserved at a time.' % self.picking_type_pick.u_max_reservable_pallets

        with self.assertRaisesRegex(ValidationError, expected_error,
                                    msg='Incorrect error thrown'):
            Batch.create_batch(picking_type_id=None,
                               picking_priorities=[],
                               user_id=self.outbound_user.id,
                               picking_id=pickings.ids)

    def test10_resets_reserved_pallet_when_closing_batch(self):
        batch = self.create_batch_with_picking()
        picking = batch.picking_ids
        batch.reserve_pallet('UDES11111', picking=picking)

        batch.close()

        self.assertFalse(picking.u_reserved_pallet)

    def test11_resets_reserved_pallet_when_removing_unfinished_work(self):
        batch = self.create_batch_with_picking()
        picking = batch.picking_ids
        batch.reserve_pallet('UDES11111', picking=picking)

        batch.remove_unfinished_work()

        self.assertFalse(picking.u_reserved_pallet)

    def test12_resets_reserved_pallet_when_unpickable_item(self):
        self.picking_type_pick.u_enable_unpickable_items = True

        batch = self.create_batch_with_picking()
        picking = batch.picking_ids
        batch.reserve_pallet('UDES11111', picking=picking)
        move_line = picking.move_line_ids[0]
        reason = 'missing item'

        batch.unpickable_item(package_name=move_line.package_id.name,
                              reason=reason)

        self.assertFalse(picking.u_reserved_pallet)
