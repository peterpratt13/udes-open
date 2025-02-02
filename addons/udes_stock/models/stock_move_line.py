# -*- coding: utf-8 -*-
from itertools import groupby
from odoo import api, fields, models, tools, _
from odoo.exceptions import UserError, ValidationError
from odoo.tools.float_utils import float_compare, float_round
from copy import deepcopy
from collections import Counter, defaultdict


class StockMoveLine(models.Model):
    _inherit = "stock.move.line"

    u_grouping_key = fields.Char("Key", compute="compute_grouping_key")

    def _get_pick_type(self):
        return self.move_id.picking_type_id.id if self.move_id else False

    u_picking_type_id = fields.Many2one(
        "stock.picking.type", "Operation Type", default=_get_pick_type
    )

    @api.constrains("move_id")
    @api.constrains("move_id.picking_type_id")
    def _set_pick_type(self):
        lines_grouped_by_type = defaultdict(lambda: self.browse())
        for line in self:
            lines_grouped_by_type[line._get_pick_type()] |= line

        for pick_type_id, lines in lines_grouped_by_type.items():
            lines.write({"u_picking_type_id": pick_type_id})

    def get_lines_todo(self):
        """ Return the move lines in self that are not completed,
            i.e., quantity done < quantity todo
        """
        return self.filtered(lambda ml: ml.qty_done < ml.product_uom_qty)

    def get_lines_done(self):
        """ Return the move lines in self that are completed,
            i.e., quantity done == quantity todo
        """
        return self.filtered(lambda ml: ml.qty_done == ml.product_uom_qty)

    def _is_done(self):
        """
        Check if all the move lines in self are considered done.
        A ml is done if the state is done, or the uom qty = qty_done.
        We check both because this function may be called before a state change, and after
        a state change the uom_qty goes to zero.
        """
        return all(ml.product_uom_qty == ml.qty_done or ml.state == "done" for ml in self)

    def _prepare_result_packages(
        self,
        package,
        result_package,
        result_parent_package,
        products_info,
        target_storage_format=None,
    ):
        """
        Compute result_package and result_parent based on the u_target_storage_format of the
        picking type (if `target_storage_format` is not supplied) + the input parameters.
        """
        Package = self.env["stock.quant.package"]

        parent_package = None

        # atm mark_as_done is only called per picking
        picking = self.mapped("picking_id")
        picking.ensure_one()

        if target_storage_format is None:
            target_storage_format = picking.picking_type_id.u_target_storage_format
        # When scan_parent_package_end=true, no need for result_package
        scan_parent_package_end = picking.picking_type_id.u_scan_parent_package_end

        Package = Package.with_context(move_line_ids=self.ids)

        if target_storage_format == "pallet_packages":

            # CASE A: both package names given
            if result_package and result_parent_package:
                result_package = Package.get_package(result_package, create=True).name
                parent_package = Package.get_package(result_parent_package, create=True)
            # CASE B: only one of the package names is given as result_package
            elif result_package or scan_parent_package_end:
                # At pallet_packages, result_package parameter is expected
                # to be the result_parent_package of the move_line
                # It might be a new pallet id
                if not scan_parent_package_end:
                    parent_package = Package.get_package(result_package, create=True)
                # MPS: maybe this if is not needed
                if not package:
                    if products_info:
                        # Products are being packed
                        result_package = Package.create({}).name
                    elif not all([ml.result_package_id for ml in self]):
                        # Setting result_parent_package expects to have
                        # result_package for all the move lines
                        raise ValidationError(
                            _("Some of the move lines don't have result package.")
                        )
                    else:
                        # We don't have either package or products and all lines have
                        # result_package_id so parent_package should be result package parameter
                        parent_package = Package.get_package(result_package, create=True)
                        result_package = None
                else:
                    # Products are being packed into a new package
                    result_package = None
                    if products_info:
                        result_package = Package.create({}).name
            # CASE C: wrong combination of package names given
            elif products_info:
                raise ValidationError(
                    _("Invalid parameters for target storage format, expecting result package.")
                )

        elif target_storage_format == "pallet_products":
            if result_package:
                # Moving stock into a pallet of products, result_package
                # might be new pallet id
                result_package = Package.get_package(result_package, create=True).name
            elif products_info and not result_package and not scan_parent_package_end:
                raise ValidationError(
                    _("Invalid parameters for target storage format, expecting result package.")
                )

        elif target_storage_format == "package":
            if products_info and not package and not result_package:
                # Mark_as_done products without package or result_package
                # Create result_package when packing products without
                # result_package being set
                result_package = Package.create({}).name

        elif target_storage_format == "product":
            # Error when trying to mark_as_done a full package or setting result package
            # when result storage format is products
            if result_package:
                raise ValidationError(
                    _("Invalid parameters for products target storage format.")
                )

        return (result_package, parent_package)

    def mark_as_done(
        self,
        location_dest=None,
        result_package=None,
        result_parent_package=None,
        package=None,
        product_ids=None,
    ):
        """ Marks as done the move lines in self and updates location_dest_id
            and result_package_id if they are set.

            When product_ids is set, only matching move lines from self will
            be marked as done for a specific quantity.

            - location_dest = string or id
            - result_package = string or id
            - package = string or id
            - product_ids = list of dictionaries, whose keys will be
                              barcode, qty, lot_names
        """
        MoveLine = self.env["stock.move.line"]
        Location = self.env["stock.location"]
        Package = self.env["stock.quant.package"]

        result_package, parent_package = self._prepare_result_packages(
            package, result_package, result_parent_package, product_ids
        )

        move_lines = self
        values = {}
        loc_dest_instance = None
        picking = None

        if location_dest is not None:
            # NB: checking if the dest loc is valid; better erroring
            # sooner than later (we'll write the new loc dest below)
            loc_dest_instance = Location.get_location(location_dest)
            picking = self.mapped("picking_id")

            if not picking.is_valid_location_dest_id(loc_dest_instance):
                raise ValidationError(
                    _(
                        "The location '%s' is not a child of the picking destination "
                        "location '%s'" % (loc_dest_instance.name, picking.location_dest_id.name)
                    )
                )

        if result_package:
            # get the result package to check if it is valid
            result_package = Package.get_package(result_package)
            values["result_package_id"] = result_package.id

        if package:
            # get the package
            package = Package.get_package(package)

        products_info_by_product = {}
        if product_ids:
            # TODO: move functions into picking instead of parameter
            picking = move_lines.mapped("picking_id")
            # prepare products_info
            products_info_by_product = move_lines._prepare_products_info(deepcopy(product_ids))
            # filter move_lines by products in producst_info_by_product and undone
            move_lines = move_lines._filter_by_products_info(products_info_by_product)
            # filter unfinished move lines
            move_lines = move_lines.get_lines_todo()
            # TODO all in one function?
            move_lines = move_lines._check_enough_quantity(
                products_info_by_product, picking=picking
            )

            if not package and move_lines.mapped("package_id"):
                raise ValidationError(_("Setting as done package operations as product operations"))

        if not move_lines:
            raise ValidationError(_("Cannot find move lines to mark as done"))

        if move_lines.filtered(lambda ml: ml.qty_done > 0):
            raise ValidationError(_("The operation is already done"))

        # check valid result_package for the move_lines that are going
        # to be marked as done only
        move_lines._assert_result_package(result_package)

        if (
            loc_dest_instance is not None
            and parent_package
            and parent_package.location_id
            and parent_package.location_id != loc_dest_instance
        ):
            # complain now rather than at validation time which could be much
            # later
            raise ValidationError(
                _("Package is already in a different location: %s in %s")
                % (parent_package.name, parent_package.location_id.name)
            )

        mls_done = MoveLine.browse()
        for ml in move_lines:
            ml_values = values.copy()
            # Check if there is specific info for the move_line product
            # otherwise we fully mark as done the move_line
            if products_info_by_product:
                # check if the qty done has been fulfilled
                if products_info_by_product[ml.product_id]["qty"] == 0:
                    continue
                ml_values, products_info_by_product = ml._prepare_line_product_info(
                    ml_values, products_info_by_product
                )
            else:
                ml_values["qty_done"] = ml.product_qty
            mls_done |= ml._mark_as_done(ml_values)

        if loc_dest_instance is not None:
            # HERE(ale): updating the dest loc here to have a single
            # invocation of its constraint handler (see below)
            mls_done.write({"location_dest_id": loc_dest_instance.id})

        # TODO: at this point products_info_by_product should be with qty_todo = 0?
        #       No necessarily, can we have add unexpected parts and not enough stock?

        # it might be useful when extending the method
        if parent_package:
            mls_done.write({"u_result_parent_package_id": parent_package.id})

        if result_package and picking is not None:
            # Print the package label
            self._trigger_print_for_pack_mark_as_done(
                active_model=picking._name,
                active_ids=picking.ids,
                print_records=result_package,
                action_filter="move_lines.mark_as_done",
            )

        if mls_done and picking is not None:
            # Print the move line label
            print_mls = MoveLine.search(
                [
                    ("result_package_id", "in", mls_done.mapped("result_package_id").ids),
                    ("product_id", "in", mls_done.mapped("product_id").ids),
                ]
            )
            self._trigger_print_for_move_line_mark_as_done(
                active_model=picking._name,
                active_ids=picking.ids,
                print_records=print_mls,
                action_filter="move_lines.mark_as_done",
            )

        return mls_done

    def _trigger_print_for_pack_mark_as_done(self, **pack_ctx):
        """Extend here to modify printing"""
        self.env.ref("udes_stock.picking_update_package").with_context(**pack_ctx).run()

    def _trigger_print_for_move_line_mark_as_done(self, **move_ctx):
        """Extend here to modify printing"""
        self.env.ref("udes_stock.picking_update_move_done").with_context(**move_ctx).run()

    def _filter_by_products_info(self, products_info):
        """ Filter the move_lines in self by the products in product_ids.
            When a product is tracked by lot/serial number:
            - when they have lot_id set, they are also filtered by
              lot number and check that they are not done
            - when they have lot_name, it is checked to avoid repeated
              lot numbers (except for lot based tracking where this is
              allowed)
        """
        # get all move lines of the products in products_info
        move_lines = self.filtered(lambda ml: ml.product_id in products_info)

        # if any of the products is tracked by lot number, filter if needed
        for product in move_lines.mapped("product_id").filtered(lambda ml: ml.tracking != "none"):
            lot_numbers = products_info[product]["lot_names"]
            repeated_lot_numbers = [sn for sn, num in Counter(lot_numbers).items() if num > 1]
            if len(repeated_lot_numbers) > 0:
                raise ValidationError(
                    _("Lot numbers %s are repeated in picking %s for product %s")
                    % (
                        " ".join(repeated_lot_numbers),
                        move_lines.mapped("picking_id").name,
                        product.name,
                    )
                )

            product_mls = move_lines.filtered(lambda ml: ml.product_id == product)
            mls_with_lot_id = product_mls.filtered(lambda ml: ml.lot_id)
            mls_with_lot_name = product_mls.filtered(lambda ml: ml.lot_name)
            if mls_with_lot_id:
                # all mls should have lot id
                if not mls_with_lot_id == product_mls:
                    raise ValidationError(
                        _("Some move lines don't have lot_id in picking %s for product %s")
                        % (product_mls.mapped("picking_id").name, product.name)
                    )

                product_mls_in_lot_numbers = mls_with_lot_id.filtered(
                    lambda ml: ml.lot_id.name in lot_numbers
                )
                if len(product_mls_in_lot_numbers) != len(lot_numbers):
                    mls_lot_numbers = product_mls_in_lot_numbers.mapped("lot_id.name")
                    diff = set(lot_numbers) - set(mls_lot_numbers)
                    if product.tracking == "serial" and set(lot_numbers) != set(mls_lot_numbers):
                        raise ValidationError(
                            _("Lot numbers %s for product %s not found in picking %s")
                            % (" ".join(diff), product.name, product_mls.mapped("picking_id").name)
                        )

                done_mls = product_mls_in_lot_numbers.filtered(lambda ml: ml.qty_done > 0)
                if product.tracking == "serial" and done_mls == product_mls_in_lot_numbers:
                    raise ValidationError(
                        _("Operations for product %s with lot numbers %s are already done.")
                        % (product.name, ",".join(done_mls.mapped("lot_id.name")))
                    )

                product_mls_not_in_lot_numbers = product_mls - product_mls_in_lot_numbers
                # remove move lines not in lot_numbers
                move_lines -= product_mls_not_in_lot_numbers

            elif mls_with_lot_name:
                # none of them has lot id, so they are new lot numbers
                product_mls_in_lot_numbers = mls_with_lot_name.filtered(
                    lambda ml: ml.lot_name in lot_numbers
                )
                if product.tracking == "serial" and product_mls_in_lot_numbers:
                    raise ValidationError(
                        _("Serial numbers %s already exist in picking %s")
                        % (
                            " ".join(product_mls_in_lot_numbers.mapped("lot_name")),
                            product_mls.mapped("picking_id").name,
                        )
                    )
                product.assert_serial_numbers(lot_numbers)
            elif product_mls:
                # new serial numbers
                product.assert_serial_numbers(lot_numbers)
            else:
                # unexpected part?
                pass

        return move_lines

    def _requires_pack_swapping(self, pack, expected_packages, product_ids):
        if not expected_packages:
            return False
        elif pack not in expected_packages:
            return True
        elif product_ids:
            # this pack is expected but we need to check if it had the quantity
            # required reserved for this pick to fulfill the request
            pick_mls_qtys = (
                self.mapped("picking_id.move_line_ids")
                .filtered(lambda ml: ml.package_id == pack)
                ._get_all_products_quantities()
            )

            product_quantities = self._prepare_products_info(deepcopy(product_ids))
            for prod, values in product_quantities.items():
                if pick_mls_qtys[prod] < int(values["qty"]):
                    return True
        else:
            return False

    def get_package_move_lines(self, package):
        """ Get move lines of package when package is a package or
            a parent package, and to handle swapping packages in
            case the expected_package_names entry is included in
            the context.

        """
        Package = self.env["stock.quant.package"]

        package.ensure_one()
        move_lines = None
        expected_package_names = self.env.context.get("expected_package_names")
        picking = self.mapped("picking_id")

        if expected_package_names is not None:

            expected_packages = Package.search([("name", "in", expected_package_names),])

            product_ids = self.env.context.get("product_ids")
            if self._requires_pack_swapping(package, expected_packages, product_ids):
                move_lines = picking.maybe_swap(package, expected_packages)

        if move_lines is None:
            if package.children_ids:
                move_lines = self.filtered(lambda ml: ml.package_id in package.children_ids)
            else:
                move_lines = self.filtered(lambda ml: ml.package_id == package)

        if not move_lines:
            raise ValidationError(
                _("Package %s not found in the operations of picking '%s'")
                % (package.name, picking.name)
            )

        return move_lines

    def _assert_result_package(self, result_package):
        """ Checks that result_package is the expected result package
            for the move lines in self. i.e., result_package has to
            match with move_line.result_package_id.
        """
        if not result_package:
            return
        for ml in self:
            ml_result_package = ml.result_package_id
            if not ml.package_id and ml_result_package and result_package != ml_result_package:
                # only when not ml.package_id because it means that what we
                # have in ml.result_package_id is the expected result package
                raise ValidationError(
                    _(
                        "A container (%s) already exists for the operation"
                        " but you are using another one (%s)"
                        % (ml_result_package.name, result_package.name)
                    )
                )

    def generate_lot_name(self, lot_names, product):
        """ If required, create a lot and return it's name in a list """
        picking_type = self.mapped("picking_id.picking_type_id")
        picking_type.ensure_one()
        confirm_tracking = picking_type.u_scan_tracking
        if confirm_tracking == "no":
            if len(lot_names) == 0:
                return [self._generate_lot_name(product)]
            else:
                return lot_names
        elif confirm_tracking == "first_last":
            raise ValidationError(_("Not implemented"))
        else:
            return lot_names

    def _generate_lot_name(self, product):
        """ Create a lot and return the name of it """
        Lots = self.env["stock.production.lot"]
        lot = Lots.create({"product_id": product.id})
        return lot.name

    def _update_products_info(self, product, products_info, info):
        """ For each (key, value) in info it merges to the corresponding
            product info if it already exists.

            where key:
                qty, lot_names

            Only for products not tracked or tracked by serial numbers

            TODO: extend this function to handle damaged
                damaged_qty, damaged_serial_numbers
        """
        tracking = product.tracking
        if tracking != "none":

            # Auto generate lot name if name is missing and required
            lot_names = info["lot_names"] if "lot_names" in info else []
            lot_names = self.generate_lot_name(lot_names, product)

            if len(lot_names) == 0:
                raise ValidationError(
                    _("Missing tracking info for product %s tracked by %s")
                    % (product.name, tracking)
                )
            info["lot_names"] = lot_names
            if tracking == "serial" and len(info["lot_names"]) != info["qty"]:
                raise ValidationError(
                    _(
                        "The number of serial numbers and quantity done"
                        " does not match for product %s"
                    )
                    % product.name
                )

        if not product in products_info:
            products_info[product] = info.copy()
        else:
            for key, value in info.items():
                if isinstance(value, int) or isinstance(value, float):
                    products_info[product][key] += value
                elif isinstance(value, list):
                    products_info[product][key].extend(value)
                else:
                    raise ValidationError(_("Unexpected type for move line parameter %s") % key)

        return products_info

    def _check_enough_quantity(self, products_info, picking=None):
        """ Check that move_lines in self can fulfill the quantity done
            in products_info, otherwise create unexpected parts if
            applicable.

            products_info is mapped by product and contains a dictionary
            with the qty to be marked as done and the list of serial
            numbers
        """
        move_lines = self
        # products_todo stores extra quantity done per product that
        # cannot be handled in the move lines in self
        products_todo = {}
        for product, info in products_info.items():
            product_mls = self.filtered(lambda ml: ml.product_id == product)
            mls_qty_reserved = sum(product_mls.mapped("product_uom_qty"))
            mls_qty_done = sum(product_mls.mapped("qty_done"))
            mls_qty_todo = mls_qty_reserved - mls_qty_done
            qty_done = info["qty"]
            diff = mls_qty_todo - qty_done
            if diff < 0:
                # not enough quantity
                products_todo[product.id] = abs(diff)

        if products_todo:
            # TODO: move function into picking?
            # if not move_line in self, there is no picking
            picking = self.mapped("picking_id") or picking
            picking = picking.with_context(lock_batch_state=True)
            new_move_lines = picking.add_unexpected_parts(products_todo)
            move_lines |= new_move_lines

        return move_lines

    def _prepare_products_info(self, product_ids):
        """ Reindex products_info by product.product model, merge repeated
            products info into one
        """
        Product = self.env["product.product"]

        products_info_by_product = {}
        for info in product_ids:
            product = Product.get_product(info["barcode"])
            del info["barcode"]
            products_info_by_product = self._update_products_info(
                product, products_info_by_product, info
            )
        return products_info_by_product

    def _prepare_line_product_info(self, values, products_info):
        """ Updates values with the proper quantity done and optionally
            with a serial number, and updates products_info according
            to it by decreasing the remaining quantity to be done
            There is an assumption that if using lot numbers/serial numbers
            that the lot names of move lines are included in the list of lot
            numbers
        """
        # TODO: extend for damaged in a different module
        self.ensure_one()

        product = self.product_id
        info = products_info[product]
        qty_done = info["qty"]

        if self.product_uom_qty < qty_done:
            qty_done = self.product_uom_qty
        values["qty_done"] = qty_done
        # update products_info remaining qty to be marked as done
        info["qty"] -= qty_done

        if product.tracking != "none":
            if self.lot_name:
                # lot_name is set when it does not exist in the system
                raise ValidationError(
                    _("Trying to mark as done a move line with lot name already set: %s")
                    % self.lot_name
                )
            # lot_id is set when it already exists in the system
            ml_lot_name = self.lot_id.name
            if ml_lot_name:
                if ml_lot_name not in info["lot_names"]:
                    raise ValidationError(
                        _("Cannot find lot number %s in the list of lot numbers to validate")
                        % ml_lot_name
                    )
            else:
                values["lot_name"] = info["lot_names"].pop()

        return (values, products_info)

    def _mark_as_done(self, values, split=True):
        """ Upate the move line with values and splits it if needed.
        """
        self.ensure_one()
        if "qty_done" not in values:
            raise ValidationError(
                _("Cannot mark as done move line %s of picking %s without quantity done")
                % (self.id, self.picking_id.name)
            )

        if split:
            self.qty_done = values["qty_done"]
            self._split()

        self.write(values)

        return self

    def _split(self):
        """ Split the move line in self if:
            - quantity done < quantity todo
            - quantity done > 0

            returns either self or the new move line
        """
        self.ensure_one()
        res = self
        qty_done = self.qty_done
        if (
            qty_done > 0
            and float_compare(
                qty_done, self.product_uom_qty, precision_rounding=self.product_uom_id.rounding
            )
            < 0
        ):
            quantity_left_todo = float_round(
                self.product_uom_qty - qty_done,
                precision_rounding=self.product_uom_id.rounding,
                rounding_method="UP",
            )
            ordered_quantity_left_todo = quantity_left_todo
            done_to_keep = qty_done
            ordered_qty = qty_done
            if qty_done > self.ordered_qty:
                ordered_qty = self.ordered_qty
                ordered_quantity_left_todo = 0

            # create new move line with the qty_done
            new_ml = self.copy(
                default={
                    "product_uom_qty": quantity_left_todo,
                    "ordered_qty": ordered_quantity_left_todo,
                    "qty_done": 0.0,
                    "result_package_id": False,
                    "u_result_parent_package_id": False,
                    "lot_name": False,
                }
            )
            # updated ordered_qty otherwise odoo will use product_uom_qty
            # new_ml.ordered_qty = ordered_quantity_left_todo
            # update self move line quantity to do
            # - bypass_reservation_update:
            #   avoids to execute code specific for Odoo UI at stock.move.line.write()
            self.with_context(bypass_reservation_update=True).write(
                {"product_uom_qty": done_to_keep, "qty_done": qty_done, "ordered_qty": ordered_qty,}
            )
            res = new_ml

        return res

    def _split_by_qty(self, qty):
        """ Split current move line in self in two move lines, where the new one
            has product_uom_qty (quantity to do) == qty

            returns either self or the new move line
        """
        # TODO: refactor with _split() making qty optional, when it is not set
        #       split by qty done
        self.ensure_one()
        res = self
        if self.qty_done > 0:
            raise ValidationError(
                _("Trying to split a move line by quantity when the move line is alreay done")
            )
        if (
            qty > 0
            and float_compare(
                qty, self.product_uom_qty, precision_rounding=self.product_uom_id.rounding
            )
            < 0
        ):
            new_ml_qty_todo = qty
            old_ml_qty_todo = float_round(
                self.product_uom_qty - qty,
                precision_rounding=self.product_uom_id.rounding,
                rounding_method="UP",
            )

            new_ml_ordered_qty = qty
            old_ml_ordered_qty = float_round(
                self.ordered_qty - qty,
                precision_rounding=self.product_uom_id.rounding,
                rounding_method="UP",
            )

            if qty > self.ordered_qty:
                new_ml_ordered_qty = 0
                old_ml_ordered_qty = self.ordered_qty

            # create new move line with the qty_done
            new_ml = self.copy(
                default={
                    "product_uom_qty": new_ml_qty_todo,
                    "ordered_qty": new_ml_ordered_qty,
                    "qty_done": 0.0,
                }
            )
            # updated ordered_qty otherwise odoo will use product_uom_qty
            # new_ml.ordered_qty = ordered_quantity_left_todo
            # update self move line quantity to do
            # - bypass_reservation_update:
            #   avoids to execute code specific for Odoo UI at stock.move.line.write()
            self.with_context(bypass_reservation_update=True).write(
                {"product_uom_qty": old_ml_qty_todo, "ordered_qty": old_ml_ordered_qty,}
            )
            res = new_ml

        return res

    def move_lines_for_qty(self, quantity, sort=True):
        """ Return a subset of move lines from self where their sum of quantity
            to do is equal to parameter quantity.
            In case that a move line needs to be split, the new move line is
            also returned (this happens when total quantity in the move lines is
            greater than quantity parameter).
            If there is not enough quantity to do in the mov lines,
            also return the remaining quantity.
        """
        new_ml = None

        # TODO: instead of sorting + filtered use a manual search to find
        #       an equal otherwise a greater. If nothing found then use all
        #       of them.

        if sort:
            sorted_mls = self.sorted(lambda ml: (ml.product_qty, ml.id))
            greater_equal_mls = sorted_mls.filtered(lambda ml: ml.product_qty >= quantity)
            # first one will be at least equal
            mls = greater_equal_mls[0] if greater_equal_mls else sorted_mls[::-1]
        else:
            mls = self

        result = self.browse()
        for ml in mls:
            if ml.product_qty >= quantity:
                extra_qty = ml.product_qty - quantity
                if extra_qty > 0:
                    new_ml = ml._split_by_qty(extra_qty)
                quantity = 0
            else:
                quantity -= ml.product_qty
            result |= ml
            if quantity == 0:
                break

        return result, new_ml, quantity

    def _get_all_products_quantities(self):
        """This function computes the different product quantities for the given move_lines
        """
        res = defaultdict(int)
        for move_line in self:
            res[move_line.product_id] += move_line.product_uom_qty
        return res

    def _prepare_info(self):
        """
            Prepares the following info of the move line self:
            - id: int
            - create_date: datetime
            - location_dest_id: {stock.lcation}
            - location_id: {stock.lcation}
            - lot_id: TBC
            - package_id: {stock.quant.package}
            - result_package_id: {stock.quant.package}
            - u_result_parent_package_id: {stock.quant.package}
            - product_uom_qty: float
            - qty_done: float
            - write_date: datetime
        """
        self.ensure_one()

        package_info = False
        result_package_info = False
        result_parent_package_info = False
        if self.package_id:
            package_info = self.package_id.get_info()[0]
        if self.result_package_id:
            result_package_info = self.result_package_id.get_info()[0]
        if self.u_result_parent_package_id:
            result_parent_package_info = self.u_result_parent_package_id.get_info()[0]

        return {
            "id": self.id,
            "create_date": self.create_date,
            "location_id": self.location_id.get_info()[0],
            "location_dest_id": self.location_dest_id.get_info()[0],
            "lot_id": self.lot_id.name,
            "package_id": package_info,
            "result_package_id": result_package_info,
            "u_result_parent_package_id": result_parent_package_info,
            "product_uom_qty": self.product_uom_qty,
            "qty_done": self.qty_done,
            "write_date": self.write_date,
        }

    def get_info(self):
        """ Return a list with the information of each move line in self.
        """
        res = []
        for line in self:
            res.append(line._prepare_info())

        return res

    def sort_by_location_product(self):
        """ Return the move lines sorted by location and product
        """
        return self.sorted(key=lambda ml: (ml.location_id.name, ml.product_id.id))

    def get_quants(self):
        """ Returns the quants related to move lines in self
        """
        Quant = self.env["stock.quant"]

        quants = Quant.browse()
        for ml in self:
            quants |= Quant._gather(
                ml.product_id,
                ml.location_id,
                lot_id=ml.lot_id,
                package_id=ml.package_id,
                owner_id=ml.owner_id,
                strict=True,
            )

        return quants

    def _prepare_task_info(self):
        """
        Prepares info of a task.
        Assumes that all the move lines of the record set are related
        to the same picking.
        """
        picking = self.mapped("picking_id")
        picking.ensure_one()
        task = {
            "picking_id": picking.id,
        }
        if picking.picking_type_id.u_reserve_pallet_per_picking:
            task.update({
                "reserved_pallet": picking.u_reserved_pallet
            })

        # Check if user_scans is manually set in context first
        user_scans = self.env.context.get("user_scans")

        if not user_scans:
            user_scans = picking.picking_type_id.u_user_scans

        if user_scans == "product":
            task["type"] = "product"
            task["pick_quantity"] = sum(self.mapped("product_qty"))
            task["quant_ids"] = self.get_quants().get_info()
        else:
            package = self.mapped("package_id")
            package.ensure_one()
            info = package.get_info(extended=True)

            if not info:
                raise ValidationError(
                    _(
                        "Expecting package information for next task to pick,"
                        " but move line does not contain it. Contact team"
                        "leader and check picking %s"
                    )
                    % picking.name
                )

            task["type"] = "package"
            task["package_id"] = info[0]

        return task

    def compute_grouping_key(self):
        # The environment must include {'compute_key': True}
        # to allow the keys to be computed.
        if not self.env.context.get("compute_key", False):
            return
        for ml in self:
            ml_vals = {
                fname: getattr(ml, fname)
                for fname in ml._fields.keys()
                if fname != "u_grouping_key"
            }
            format_str = ml.picking_id.picking_type_id.u_move_line_key_format

            if format_str:
                ml.u_grouping_key = format_str.format(**ml_vals)
            else:
                ml.u_grouping_key = None

    def group_by_key(self):

        if any(
            pt.u_move_line_key_format is False for pt in self.mapped("picking_id.picking_type_id")
        ):
            raise UserError(
                _("Cannot group move lines when their picking type has no grouping key set.")
            )

        by_key = lambda ml: ml.u_grouping_key
        return {
            k: self.browse([ml.id for ml in g])
            for k, g in groupby(sorted(self.with_context(compute_key=True), key=by_key), key=by_key)
        }

    def write(self, values):
        """Extend write to catch preprocessed location update as constrains
        happens after write creating a check which can never fail
        """
        if "location_dest_id" in values and self.mapped(
            "picking_id.picking_type_id.u_drop_location_preprocess"
        ):

            location = self.env["stock.location"].browse(values["location_dest_id"])
            self._validate_location_dest(location=location)

        # By default bypass reservation update:
        #   Avoids to execute code specific for Odoo UI at stock.move.line.write()
        #   In case product_uom_qty is in values, check the context variable, since
        #   some code relies on changing product_uom_qty to unreserve quants.
        if "bypass_reservation_update" not in self.env.context and "product_uom_qty" not in values:
            bypass = True
        else:
            bypass = self.env.context.get("bypass_reservation_update", False)

        return super(StockMoveLine, self.with_context(bypass_reservation_update=bypass)).write(
            values
        )

    ## Drop Location Constraint

    @api.constrains("location_dest_id")
    @api.onchange("location_dest_id")
    def _validate_location_dest(self, location=None):
        """ Ensures that the location destination is a child of the
            default_location_dest_id of the picking and that is
            one of the suggested locations if the drop off policy
            is 'enforce' or 'enforce_with_empty'.
        """
        Users = self.env["res.users"]

        if location is None:
            location = self.mapped("location_dest_id")

        # On create we need to allow views to pass.
        # On other actions if this will be caught by odoo's code as you are not
        # allowed to place stock in a view.
        if set(location.mapped("usage")) == set(("view",)):
            return

        # iterating picking_id even if it's a many2one
        # because the constraint can be triggered anywhere
        for picking in self.mapped("picking_id"):
            if not picking.is_valid_location_dest_id(location=location):
                raise ValidationError(
                    _(
                        "The location '%s' is not a child of the picking "
                        "destination location '%s'" % (location.name, picking.location_dest_id.name)
                    )
                )

            constraint = picking.picking_type_id.u_drop_location_constraint

            if constraint in ["enforce", "enforce_with_empty"]:
                # Don't use move lines that are not available for suggesting
                mls = self.filtered(
                    lambda ml: ml.picking_id == picking
                    and ml.state in ("assigned", "partially_available")
                )

                if not mls:
                    continue

                # location should be one of the suggested locations
                locations = picking.get_suggested_locations(mls, sort=False)

                # or an empty location
                if constraint == "enforce_with_empty":
                    locations = locations | picking.get_empty_locations(sort=False)

                # or the damaged stock location if the picking type is set up
                # to handle damages
                warehouse = Users.get_user_warehouse()
                if picking.picking_type_id in warehouse.u_handle_damages_picking_type_ids:
                    locations = locations | warehouse.u_damaged_location_id

                # location should be one of the suggested locations, if any
                if locations and location not in locations:
                    raise ValidationError(
                        _("Drop off location must be one of the suggested locations")
                    )

    def any_destination_locations_default(self):
        """Checks if all location_dest_id's are the picks default
           location_dest_id of the picking.
        """
        default_dest = self.mapped("picking_id.location_dest_id")
        default_dest.ensure_one()
        return any(ml.location_dest_id == default_dest for ml in self)

    def new_package_name(self):
        """ Given a move line compute the next package name according to
            the policy assigned to the its picking picking type.
            If no policy is assigned the default policy is applied.
        """
        picking_type = self.mapped("picking_id.picking_type_id")
        picking_type.ensure_one()
        policy = picking_type.u_new_package_policy
        name = None
        if policy:
            func = getattr(self, "new_package_name_" + policy)
            name = func()
        # Always return a name
        if not name:
            name = self.new_package_name_default()

        return name

    def new_package_name_default(self):
        return self.env["stock.quant.package"].new_package_name()

    def _drop_off_criterion_summary(self):
        """ Generate product summary for drop off criterion for the move
            lines in self.
            Generate one piece of information for each product:
            * Display name
            * Total quantity in move lines
            * Speed of the product (if it is set)
        """
        self.mapped("product_id")
        summary = ""
        for product, prod_mls in self.groupby(lambda ml: ml.product_id):
            product_speed = ""
            if product.u_speed_category_id:
                product_speed = " (speed: {})".format(product.u_speed_category_id.name)
            summary += "<br>{} x {}{}".format(
                product.display_name, int(sum(prod_mls.mapped("qty_done"))), product_speed
            )
        return summary

    @api.constrains(
        "product_id",
        "location_id",
        "location_dest_id",
        "package_id",
        "result_package_id",
        "lot_id",
        "lot_name",
        "qty_done",
    )
    def _prevent_write_after_done(self):
        done_lines = self.filtered(lambda x: x.state == "done")
        if done_lines:
            raise ValidationError(_("Cannot update move lines that are already 'done'."))

    @api.model_cr
    def init(self):
        """ Creates indexes for _check_resultant_package_level
        """
        super(StockMoveLine, self).init()

        tools.create_index(
            self._cr,
            "stock_move_line_state_result_package_id_index",
            self._table,
            ["state", "result_package_id"],
        )
        tools.create_index(
            self._cr,
            "stock_move_line_state_u_result_parent_package_id_index",
            self._table,
            ["state", "u_result_parent_package_id"],
        )

    @api.constrains("result_package_id", "u_result_parent_package_id")
    @api.onchange("result_package_id", "u_result_parent_package_id")
    def _check_resultant_package_level(self):
        MoveLine = self.env["stock.move.line"]
        # Collect move lines with packages related to those being checked which are in progress
        if not self.mapped("result_package_id"):
            return
        package_ids = self.mapped("result_package_id") | self.mapped("u_result_parent_package_id")
        package_mls = MoveLine.search(
            [
                ("state", "=", "assigned"),
                "|",
                ("result_package_id", "in", package_ids.mapped("id")),
                ("u_result_parent_package_id", "in", package_ids.mapped("id")),
            ]
        )

        for ml in self:
            storage_format = ml.u_picking_type_id.u_target_storage_format
            result_package_is_parent = package_mls.filtered(
                lambda package_ml: ml.result_package_id == package_ml.u_result_parent_package_id
            )
            if storage_format == "product" and (
                ml.u_result_parent_package_id or ml.result_package_id
            ):
                raise ValidationError(_("Pickings stored by product cannot be inside packages."))
            elif storage_format in ["package", "pallet_products"] and (
                result_package_is_parent
                or ml.u_result_parent_package_id
                or ml.result_package_id.u_package_depth >= 2
            ):
                raise ValidationError(
                    _(
                        "Pickings stored by package or pallet of products cannot have a parent package."
                    )
                )
            elif storage_format == "pallet_packages" and (
                (
                    result_package_is_parent
                    or (
                        package_mls.filtered(
                            lambda package_ml: ml.u_result_parent_package_id
                            == package_ml.result_package_id
                        )
                    )
                    or ml.result_package_id.u_package_depth >= 2
                )
            ):
                raise ValidationError(_("Maximum package depth exceeded."))

    def _split_and_group_mls_by_quantity(self, maximum_qty):
        """Split move lines into groups of up to a maximum quantity"""
        grouped_mls = []

        # Group all (partially) done and cancelled moves first
        excluded_move_lines = self.filtered(lambda m: m.qty_done or m.state == "done")
        if excluded_move_lines:
            grouped_mls.append(excluded_move_lines)
            self -= excluded_move_lines

        # Sse if any mls are equal to the maximum and add them as individual groups
        exact_mls = self.filtered(lambda l: l.product_uom_qty == maximum_qty)
        self -= exact_mls
        for ml in exact_mls:
            grouped_mls.append(ml)

        # Next try splitting and grouping to maintain a single ml per group:
        for ml in self:
            quantity = ml.product_uom_qty
            if quantity > maximum_qty:
                num_full_move_lines = int(quantity // maximum_qty)
                remainder = quantity % maximum_qty
                for i in range(num_full_move_lines):
                    new_ml = ml._split_by_qty(maximum_qty)
                    grouped_mls.append(new_ml)
                # If there is not a remainder, ml has already been grouped
                # so remove from self
                if not remainder:
                    self -= ml

        # Finally split and group the remainder to meet the maximum
        quantity = 0
        movelines = self.browse()
        for ml in self:
            difference = maximum_qty - quantity
            if quantity <= difference:
                quantity += ml.product_uom_qty
                movelines += ml
                if quantity == maximum_qty:
                    grouped_mls.append(movelines)
                    quantity = 0
                    movelines = self.browse()
            else:
                # Need to split a line to reach the maximum without exceeding it
                remainder = ml.product_uom_qty - difference
                new_ml = ml._split_by_qty(remainder)
                movelines =+ ml
                grouped_mls.append(movelines)
                # Use the other split ml as the first ml in a new group
                # (will allways be less than maximum due to previous step)
                grouped_mls = new_ml
                quantity = new_ml.product_uom_qty

        # Any remainder should be in their own group
        if movelines:
            grouped_mls.append(movelines)

        return grouped_mls
