# -*- coding: utf-8 -*-
import inspect
from collections import OrderedDict, defaultdict
from lxml import etree

from odoo import api, fields, models, SUPERUSER_ID, _
from odoo.exceptions import ValidationError, UserError

from ..common import check_many2one_validity
from . import common
from .. import utils

import logging

import time
import re

from psycopg2 import OperationalError, errorcodes

_logger = logging.getLogger(__name__)

PG_CONCURRENCY_ERRORS_TO_RETRY = (
    errorcodes.LOCK_NOT_AVAILABLE,
    errorcodes.SERIALIZATION_FAILURE,
    errorcodes.DEADLOCK_DETECTED,
)
MAX_TRIES_ON_CONCURRENCY_FAILURE = 5


def allow_preprocess(func):
    func._allow_preprocess = True
    return func


class StockPicking(models.Model):
    _inherit = "stock.picking"

    _order = "priority desc, scheduled_date asc, sequence asc, id asc"

    # Adding fields to be hidden by default from form view.
    DetailedFormViewFields = [
        "u_first_picking_ids",
        "u_prev_picking_ids",
        "u_next_picking_ids",
        "u_created_back_orders",
        "u_first_pickings_qty",
        "u_prev_pickings_qty",
        "u_next_pickings_qty",
        "u_back_orders_qty",
    ]

    priority = fields.Selection(selection=common.PRIORITIES)
    sequence = fields.Integer("Sequence", default=0)
    is_locked = fields.Boolean(
        compute="_compute_is_locked",
        store=True,
        help="When the picking is not done this allows changing the "
        "initial demand. When the picking is done this allows "
        "changing the done quantities.",
    )

    u_mark = fields.Boolean(
        "Marked",
        default=True,
        oldname="active",
        index=True,
        help="True if picking is in use. False indicates the picking can be deleted.",
    )

    # compute previous and next pickings
    u_prev_picking_ids = fields.One2many(
        "stock.picking",
        string="Previous Pickings",
        compute="_compute_related_picking_ids",
        help="Previous pickings",
    )
    u_prev_pickings_qty = fields.Integer(
        "Previous Pickings Qty",
        compute="_compute_related_picking_ids",
        help="Previous pickings quantity",
    )
    u_next_picking_ids = fields.One2many(
        "stock.picking",
        string="Next Pickings",
        compute="_compute_related_picking_ids",
        help="Next pickings",
    )
    u_next_pickings_qty = fields.Integer(
        "Next Pickings Qty",
        compute="_compute_related_picking_ids",
        help="Next pickings quantity",
    )
    u_first_picking_ids = fields.One2many(
        "stock.picking",
        string="First Pickings",
        compute="_compute_first_picking_ids",
        help="First pickings in the chain",
    )
    u_first_pickings_qty = fields.Integer(
        "First Pickings Qty",
        compute="_compute_first_picking_ids",
        help="First pickings quantity",
    )
    u_created_back_orders = fields.One2many(
        "stock.picking",
        string="Created Back Orders",
        compute="_compute_related_picking_ids",
        help="Created Back Orders",
    )
    u_back_orders_qty = fields.Integer(
        "Created Back Orders Qty",
        compute="_compute_related_picking_ids",
        help="Created back orders quantity",
    )
    u_original_picking_id = fields.Many2one(
        "stock.picking",
        string="Original Picking",
        help="The original picking of the refactored/backordered picking",
        copy=True,
    )

    # search helpers for source and destination package
    u_package_id = fields.Many2one(
        "stock.quant.package",
        "Package",
        related="move_line_ids.package_id",
        help="Source package (used to search on pickings)",
    )
    u_result_package_id = fields.Many2one(
        "stock.quant.package",
        "Result Package",
        related="move_line_ids.result_package_id",
        help="Destination package (used to search on pickings)",
    )
    u_pending = fields.Boolean(compute="_compute_pending")

    # override batch_id to be copied
    batch_id = fields.Many2one("stock.picking.batch", copy=True)

    u_batch_user_id = fields.Many2one(
        string="Batch User",
        related="batch_id.user_id",
        store=False,
        help="User responsible for the batch",
    )

    u_quantity_done = fields.Float(
        "Quantity done",
        compute="_compute_picking_quantities",
        digits=(0, 0),
        store=False,
        help="Quantity done of the moves related to the picking",
    )

    u_total_quantity = fields.Float(
        "Total quantity",
        compute="_compute_picking_quantities",
        digits=(0, 0),
        store=False,
        help="Total quantity todo of the moves related to the picking",
    )

    u_has_discrepancies = fields.Boolean(
        string="Has discrepancies",
        compute="_compute_picking_quantities",
        readonly=True,
        help="Flag to indicate if the picking still has discrepancies.",
    )

    u_num_pallets = fields.Integer(
        "Total Pallets count",
        compute="_compute_picking_packages",
        store=False,
        help="Total number of different pallets in the picking",
    )

    u_num_packages = fields.Integer(
        "Total Packages count",
        compute="_compute_picking_packages",
        store=False,
        help="Total number of different packages in the picking",
    )

    u_location_category_id = fields.Many2one(
        comodel_name="stock.location.category",
        compute="_compute_location_category",
        string="Location Category",
        help="Used to know which pickers have the right equipment to pick it. "
        "In case multiple location categories are found in the picking it "
        "will be empty.",
        readonly=True,
        store=True,
    )

    u_reserved_pallet = fields.Char(
        string="Reserved Pallet",
        index=True,
        copy=False,
        help="If reserving pallets per picking is enabled, this field stores "
        "the pallet reserved for this picking.",
    )

    @api.model
    def create(self, vals):
        """
        Add the original picking id on creation because of race conditions
        of computed fields.
        For example, when creating a backorder,
        the backorder gets related but u_prev_picking_ids gets done after.
        So instead, we just write to a new field which we copy across backordered pickings
        """
        picking = super().create(vals)
        if not picking.backorder_id:
            picking.write({"u_original_picking_id": picking.id})
        return picking

    def copy(self, default=None):
        """Set `planned_picking` to True in context"""
        self = self.with_context(planned_picking=True)
        return super().copy(default)

    @api.depends("move_line_ids", "move_line_ids.location_id")
    @api.one
    def _compute_location_category(self):
        """ Compute location category from move lines"""
        if self.move_line_ids:
            categories = self.move_line_ids.mapped("location_id.u_location_category_id")
            self.u_location_category_id = categories if len(categories) == 1 else False

    @api.depends("move_lines", "move_lines.quantity_done", "move_lines.ordered_qty")
    @api.one
    def _compute_picking_quantities(self):
        """ Compute the quantity done and to do of the picking from the moves"""
        total_qty_done = 0.0
        total_qty_todo = 0.0
        has_discrepancies = False
        for move in self.move_lines.filtered(lambda ml: ml.state != "cancel"):
            qty_done = move.quantity_done
            qty_todo = move.ordered_qty
            if qty_done != qty_todo:
                has_discrepancies = True
            total_qty_done += qty_done
            total_qty_todo += qty_todo

        self.u_quantity_done = total_qty_done
        self.u_total_quantity = total_qty_todo
        self.u_has_discrepancies = has_discrepancies

    @api.depends("move_line_ids", "move_line_ids.result_package_id")
    @api.one
    def _compute_picking_packages(self):
        """ Compute the number of pallets and packages from the operations """
        User = self.env["res.users"]

        warehouse = User.get_user_warehouse()
        pallet_regex = warehouse.u_pallet_barcode_regex
        package_regex = warehouse.u_package_barcode_regex

        all_packages = self.move_line_ids.mapped("result_package_id") | self.move_line_ids.mapped(
            "u_result_parent_package_id"
        )
        num_pallets = 0
        num_packages = 0
        if pallet_regex:
            pallets = all_packages.filtered(lambda p: re.match(pallet_regex, p.name))
            num_pallets = len(pallets)
        self.u_num_pallets = num_pallets

        if package_regex:
            packages = all_packages.filtered(lambda p: re.match(package_regex, p.name))
            num_packages = len(packages)
        self.u_num_packages = num_packages

    # Calculate previous/next pickings
    @api.depends(
        "move_lines",
        "move_lines.move_orig_ids",
        "move_lines.move_dest_ids",
        "move_lines.move_orig_ids.picking_id",
        "move_lines.move_dest_ids.picking_id",
    )
    def _compute_related_picking_ids(self):
        Picking = self.env["stock.picking"]
        if self.ids:
            # To avoid messy ORM joins, get next/prev picking ids
            # with a direct query for all picks in self
            self.env.cr.execute(
                """
                SELECT
                    sp.id,
                    array_agg(DISTINCT next_pick.id) AS next_picks,
                    array_agg(DISTINCT prev_pick.id) AS prev_picks,
                    array_agg(DISTINCT backorder.id) AS backorder_picks
                FROM
                    stock_picking sp
                    LEFT JOIN stock_picking backorder ON backorder.backorder_id = sp.id
                    LEFT JOIN stock_move sm ON sm.picking_id = sp.id
                    LEFT JOIN stock_move_move_rel dest_move_rel ON sm.id = dest_move_rel.move_orig_id
                    LEFT JOIN stock_move_move_rel orig_move_rel ON sm.id = orig_move_rel.move_dest_id
                    LEFT JOIN stock_move orig_move ON orig_move.id = orig_move_rel.move_orig_id
                    LEFT JOIN stock_move dest_move ON dest_move.id = dest_move_rel.move_dest_id
                    LEFT JOIN stock_picking next_pick ON dest_move.picking_id = next_pick.id
                    LEFT JOIN stock_picking prev_pick ON orig_move.picking_id = prev_pick.id
                WHERE
                    sp.id IN %s
                GROUP BY sp.id
            """,
                (tuple(self.ids),),
            )
            for pick_id, next_pick_ids, prev_pick_ids, backorder_ids in self.env.cr.fetchall():
                picking = Picking.browse(pick_id)
                # Cleanup empty entries, will only ever be one None due to DISTINCT
                next_pick_ids = list(filter(None, next_pick_ids))
                prev_pick_ids = list(filter(None, prev_pick_ids))
                backorder_ids = list(filter(None, backorder_ids))
                # Set the computed columns
                picking.u_next_picking_ids = next_pick_ids
                picking.u_prev_picking_ids = prev_pick_ids
                picking.u_created_back_orders = backorder_ids
                picking.u_next_pickings_qty = len(next_pick_ids)
                picking.u_prev_pickings_qty = len(prev_pick_ids)
                picking.u_back_orders_qty = len(backorder_ids)

    @api.depends("move_lines", "move_lines.move_orig_ids", "move_lines.move_orig_ids.picking_id")
    def _compute_first_picking_ids(self):
        for picking in self:
            first_moves = self.env["stock.move"].browse()
            moves = picking.move_lines
            while moves:
                first_moves |= moves.filtered(lambda x: not x.move_orig_ids)
                moves = moves.mapped("move_orig_ids")
            first_pickings = first_moves.mapped("picking_id")
            picking.u_first_picking_ids = first_pickings
            picking.u_first_pickings_qty = len(first_pickings)

    def can_handle_partials(self, **kwargs):
        self.ensure_one()
        return self.picking_type_id.u_handle_partials

    def _compute_pending(self):
        """Compute if a picking is pending.
        Pending means it has previous pickings that are not yet completed.
        Skip the calculation if the picking type is allowed to handle partials.
        """
        for picking in self:
            if picking.can_handle_partials() is False:
                prev_pickings_states = picking.u_prev_picking_ids.mapped("state")
                picking.u_pending = (
                    "waiting" in prev_pickings_states
                    or "assigned" in prev_pickings_states
                    or "confirmed" in prev_pickings_states
                )
            else:
                picking.u_pending = False

    @api.depends("move_type", "move_lines.state", "move_lines.picking_id")
    @api.one
    def _compute_state(self):
        """Prevent pickings to be in state assigned when not able to handle
        partials, so they should remain in state waiting or confirmed until
        they are fully assigned.

        Add the flag 'computing_state' when we call can_handle_partials here to
        distinguish it from other calls.
        """
        move_lines = self.move_lines.filtered(lambda move: move.state not in ["cancel", "done"])
        if move_lines and not self.can_handle_partials(computing_state=True):
            relevant_move_state = move_lines._get_relevant_state_among_moves()
            if relevant_move_state == "partially_available":
                if self.u_prev_picking_ids:
                    self.state = "waiting"
                else:
                    self.state = "confirmed"
                return

        super()._compute_state()

    @api.depends("state")
    @api.one
    def _compute_is_locked(self):
        """Derive is_locked from the picking's state."""
        self.is_locked = self.state != "draft"

    @api.model
    def _fields_view_get(self, view_id=None, view_type="form", toolbar=False, submenu=False):
        result = super(StockPicking, self)._fields_view_get(
            view_id=view_id, view_type=view_type, toolbar=toolbar, submenu=submenu
        )

        # Disable the Delete button for untrusted users
        group_trusted_user = self.env.ref("udes_security.group_trusted_user")
        if group_trusted_user not in self.env.user.groups_id:
            node = etree.fromstring(result["arch"])
            if node.tag in ("kanban", "tree", "form", "gantt"):
                node.set("delete", "false")
                result["arch"] = etree.tostring(node, encoding="unicode")
        # Set Autofocus on Additional Info
        if self._context.get("view_all_fields"):
            doc = etree.XML(result["arch"])
            for node in doc.xpath("//page[@name='extra']"):
                node.set("autofocus", "autofocus")
            result["arch"] = etree.tostring(doc)
        return result

    def assert_not_pending(self):
        for picking in self:
            if picking.can_handle_partials() is False and picking.u_pending is True:
                raise UserError(
                    _("Cannot validate %s until all of its" " preceding pickings are done.")
                    % picking.name
                )

    def assert_not_lot_restricted(self):
        """
        Assert that the pickings in self are not restricted based on lot.
        If picking operation type restricts multi lot transfers then check for number of unique
        lots in move lines and raise an error if picking contains more than one. Pickings that
        contain a mix of set lot and empty lot move lines will also raise an error.
        """
        restrict_multi_lot_pickings = self.filtered(
            lambda p: p.picking_type_id.u_restrict_multi_lot_pickings
        )

        for pick in restrict_multi_lot_pickings:
            # Get the move lines tracked by lot or by none
            non_serial_move_line_ids = pick.move_line_ids.filtered(
                lambda ml: ml.product_id.tracking != "serial"
            )

            if non_serial_move_line_ids:
                # Count the number of unique lots in the move lines
                lots = non_serial_move_line_ids.mapped("lot_id")
                lot_count = len(lots)

                if lot_count:
                    if lot_count > 1:
                        raise ValidationError(_("Cannot validate transfer with multiple lots."))

                    # Count the move lines without a lot set
                    empty_lot_move_line_ids = non_serial_move_line_ids.filtered(
                        lambda ml: not ml.lot_id
                    )
                    empty_lot_count = len(empty_lot_move_line_ids)

                    if lot_count and empty_lot_count:
                        raise ValidationError(
                            _(
                                "Cannot validate transfer with both set lot and empty lot "
                                "move lines."
                            )
                        )

    def action_assign(self):
        """
        Unlink empty pickings after action_assign, as there may be junk data
        after a refactor
        """
        res = super(StockPicking, self).action_assign()
        if self:
            self.unlink_empty()
        return res

    def action_done(self):
        """
        Ensure we don't incorrectly validate pending pickings.
        Check if picking batch is now complete
        """
        self.assert_not_pending()
        mls = self.mapped("move_line_ids")
        # Prevent recomputing the batch stat
        batches = mls.mapped("picking_id.batch_id")
        next_pickings = self.mapped("u_next_picking_ids")

        res = super(StockPicking, self.with_context(lock_batch_state=True)).action_done()

        # just in case move lines change on action done, for instance cancelling
        # a picking
        mls = mls.exists()
        picks = mls.mapped("picking_id")
        # batches of the following stage should also be recomputed
        all_picks = picks | picks.mapped("u_next_picking_ids")
        all_picks.with_context(lock_batch_state=False)._trigger_batch_state_recompute()

        self.assert_not_lot_restricted()

        extra_context = {}
        if hasattr(self, "get_extra_printing_context"):
            extra_context = picks.get_extra_printing_context()

        # Trigger print strategies for pickings done
        self.env.ref("udes_stock.picking_done").with_context(
            active_model=picks._name,
            active_ids=picks.ids,
            action_filter="picking.action_done",
            **extra_context
        ).run()
        if self:
            (self | next_pickings).unlink_empty()
        return res

    def action_cancel(self):
        """
        Check if picking batch is now complete
        """
        batch = self.mapped("batch_id")
        res = super(StockPicking, self.with_context(lock_batch_state=True)).action_cancel()
        batch._compute_state()
        return res

    def write(self, vals):
        """If writing picking, check if previous batch is now complete"""
        # This will be used to trigger recompute of the batch state
        # we can't relate on the state after as batch_id might be
        # removed in the write
        batches = self.mapped(lambda p: p.batch_id)
        context_vals = {"orig_batches": batches} if batches else {}
        return super(StockPicking, self.with_context(**context_vals)).write(vals)

    def button_validate(self):
        """Ensure we don't incorrectly validate pending pickings.

        Additionally, display a warning message if the defined condition is not met.
        """
        self.ensure_one()

        self.assert_not_pending()
        self.update_lot_names()

        # Run picking checks and display warning
        warning_message = self.action_warn_picking_precondition()
        if warning_message:
            return {
                "name": _(warning_message),
                "type": "ir.actions.act_window",
                "view_type": "form",
                "view_mode": "form",
                "res_model": "stock.picking",
                "views": [(self.env.ref("udes_stock.view_picking_warning").id, "form")],
                "view_id": self.env.ref("udes_stock.view_picking_warning").id,
                "target": "new",
                "res_id": self.id,
                "context": self.env.context,
            }

        return super(StockPicking, self).button_validate()

    def button_validate_continue(self):
        """Method to continue with the button validate method after a warning message."""
        return super(StockPicking, self).button_validate()

    def update_lot_names(self):
        """ Create lot names for move lines where user is not required to provide them """
        picking_type = self.picking_type_id
        if (
            picking_type.use_create_lots or picking_type.use_existing_lots
        ) and picking_type.u_scan_tracking == "no":
            lines_to_check = self.move_line_ids.filtered(
                lambda ml: ml.product_id.tracking != "none"
            )
            for line in lines_to_check:
                product = line.product_id
                if not line.lot_name and not line.lot_id:
                    line.lot_name = line._generate_lot_name(product)

    def assert_valid_state(self):
        """Checks if the transfer is in a valid state, i.e., not done or cancel
        otherwise it raises and error
        """
        self.ensure_one()
        if self.state in ["done", "cancel"]:
            raise ValidationError(_("Wrong state of picking %s") % self.state)

    def add_unexpected_parts(self, product_quantities):
        """By default allow to overreceive and it will be extended in
        a module where the picking type has a flag to decide this.
        """
        Product = self.env["product.product"]

        self.ensure_one()

        if self.picking_type_id.u_over_receive:
            old_move_lines = self.move_line_ids
            self._create_moves(product_quantities, confirm=True, assign=True, unexpected=True)
            new_move_lines = self.move_line_ids - old_move_lines

            for ml in old_move_lines:
                if ml.qty_done > 0 and ml.product_uom_qty > ml.qty_done:
                    # Note: when adding extra moves/move_lines, odoo will merge
                    # them into one of the existing ones, therefore we need
                    # to split the old_move_lines that have qty_done > 0 and
                    # qty_done < product_uom_qty

                    # TODO: handle this properly at odoo core:
                    #       avoid to merge new move lines if qty_done > 0?

                    # new_ml has qty_done 0 and ml is done
                    new_ml = ml._split()
                    new_move_lines |= new_ml

            # These are unexpected so the ordered_qty should be
            new_move_lines.write({"ordered_qty": 0})
        else:
            overreceived_qtys = [
                "%s: %s" % (Product.get_product(id).name, qty)
                for id, qty in product_quantities.items()
            ]
            raise ValidationError(
                _(
                    "We are not expecting these extra quantities of"
                    " these parts:\n%s\nPlease either receive the right"
                    " amount and move the rest to probres, or if they"
                    " cannot be split, move all to probres."
                )
                % "\n".join(overreceived_qtys)
            )

        return new_move_lines

    def _create_moves_from_quants(self, quant_ids, **kwargs):
        """Check that the quants are valid to be used in self and
        create moves calling _create_moves().
        """
        Quant = self.env["stock.quant"]

        allow_partial = self.env.context.get("allow_partial")

        self.assert_valid_state()
        if isinstance(quant_ids, list):
            quants = Quant.browse(quant_ids)
        elif isinstance(quant_ids, type(Quant)):
            quants = quant_ids
        else:
            raise ValidationError(_("Wrong quant identifiers %s") % type(quant_ids))
        n_quants = len(quants.exists())
        n_quants_rec = len(quant_ids)
        if n_quants != n_quants_rec:
            raise AssertionError(
                _(
                    "Number of quants provided %s does not match with "
                    "the number of quants found %s. Data received: %s"
                )
                % (n_quants_rec, n_quants, quant_ids)
            )
        if not allow_partial and self.picking_type_id.u_target_storage_format != "product":
            quants.assert_not_reserved()
            quants.assert_entire_packages()
        quants.assert_valid_location(self.location_id.id)

        # recompute quant_ids from quants recordset
        quant_ids = quants.ids
        # Call _create_moves() with context variable quants_ids in order
        # to filter the quants that stock.quant._gather returns
        self.with_context(quant_ids=quant_ids)._create_moves(
            quants.group_quantity_by_product(only_available=allow_partial), **kwargs
        )

    def _create_moves(
        self,
        products_info,
        values=None,
        confirm=False,
        assign=False,
        result_package=None,
        unexpected=False,
        product_quantity=None,
    ):
        """Creates moves from products_info and adds it to the picking
        in self. Where products_info is a dictionary mapped by
        product ids and the value are the quantities.

        The picking is also confirmed/assigned if the flags are set to True.
        If result_package is set, it will update the result_package_id of the
        new move_lines when assign flag is True.
        """
        Product = self.env["product.product"]
        Move = self.env["stock.move"]
        Package = self.env["stock.quant.package"]

        self.assert_valid_state()

        if values is None:
            values = {}
        values["picking_id"] = self.id
        if not "location_id" in values:
            values["location_id"] = self.location_id.id
        if not "location_dest_id" in values:
            values["location_dest_id"] = self.location_dest_id.id
        if not "picking_type_id" in values:
            values["picking_type_id"] = self.picking_type_id.id
        if not "group_id" in values:
            values["group_id"] = self.group_id.id
        if not "product_uom" in values:
            default_uom_id = self.env.ref("product.product_uom_unit").id
            values["product_uom"] = default_uom_id

        for product_id, qty in products_info.items():
            if product_quantity:
                qty = product_quantity
            if not qty:
                raise ValidationError(
                    _("No quantity specified creating move for product %s") % product_id
                )
            move_vals = {
                "name": "{} {}".format(qty, Product.browse(product_id).display_name),
                "product_id": product_id,
                "product_uom_qty": qty,
                "date_expected": self.scheduled_date,
            }
            move_vals.update(values)
            move = Move.create(move_vals)
            if unexpected:
                move.ordered_qty = 0

        if confirm:
            # Use picking.action_confirm, which will merge moves of the same
            # product. In case that is not wanted use moves._action_confirm(merge=False)
            # Use sudo() to allow unlink of the merged moves
            self.sudo().action_confirm()

        if assign:
            old_move_line_ids = self.move_line_ids
            # Use picking.action_assign or moves._action_assign to create move lines
            # with context variable bypass_reservation_update in order to avoid
            # to execute code specific for Odoo UI at stock.move.line.write()
            self.with_context(bypass_reservation_update=True).action_assign()
            if result_package:
                # update result_package_id of the new move_line_ids
                package = Package.get_package(result_package)
                new_move_line_ids = self.move_line_ids - old_move_line_ids
                new_move_line_ids.write({"result_package_id": package.id})

    def create_picking(
        self,
        quant_ids,
        location_id,
        picking_type_id=None,
        location_dest_id=None,
        result_package_id=None,
        move_parent_package=False,
        origin=None,
        group_id=None,
        product_quantity=None,
        create_batch=False,
    ):
        """Creates a stock.picking and stock.moves for a given list
        of stock.quant ids

        @param quant_ids: Array (int)
            An array of the quants ID to add to the stock.picking
        @param location_id: int
            ID of the location where the stock.picking is moving from.
        @param (optional) picking_type_id: int
            The type of the stock.picking.
        @param (optional) location_dest_id: int
            ID of the location where the stock is going to be moved to.
        @param (optional) result_package_id: int
            The target package ID
        @param (optional) move_parent_package: Boolean
            Used in pallets/nested packages, to maintain the move of the entire pallet.
            Defaults to False
        @param (optional) origin: string
            Value of the source document of the new picking
        @param (optional) group_id: int
            ID of the group where the stock is being assigned to
        @param (optional) product_quantity: int
            Quantity to be moved during internal transfer of loose product rather than whole quant.
        @param (optional) create_batch: Boolean
            Used to assign the picking to a batch for the current user e.g. for internal transfer.
        """
        Picking = self.env["stock.picking"]
        PickingType = self.env["stock.picking.type"]
        Location = self.env["stock.location"]
        Users = self.env["res.users"]
        PickingBatch = self.env["stock.picking.batch"]

        # get picking_type from picking_type_id or internal transfer
        if picking_type_id:
            picking_type = PickingType.get_picking_type(picking_type_id)
        else:
            warehouse = Users.get_user_warehouse()
            picking_type = warehouse.int_type_id

        if not location_dest_id:
            location_dest_id = picking_type.default_location_dest_id.id

        # check params
        for (field, obj, id_) in [
            ("location_id", Location, location_id),
            ("location_dest_id", Location, location_dest_id),
        ]:
            check_many2one_validity(field, obj, id_)

        # Create stock.picking
        values = {
            "location_id": location_id,
            "location_dest_id": location_dest_id,
            "picking_type_id": picking_type.id,
        }

        if origin:
            values["origin"] = origin
        if group_id is not None:
            values["group_id"] = group_id
        picking = Picking.create(values.copy())

        # Create stock.moves
        picking._create_moves_from_quants(
            quant_ids,
            values=values.copy(),
            confirm=True,
            assign=True,
            result_package=result_package_id,
            product_quantity=product_quantity,
        )

        # TODO: this might be in the package_hierarchy module, because odoo by default
        #       does not handle parent packages
        if not move_parent_package:
            # not needed yet
            # when false remove parent_id of the result_package_id ??
            # picking.move_line_ids.mapped('result_package_id').write({'package_id': False})
            pass

        if create_batch:
            batch = PickingBatch.create_batch(picking_type_id, None, picking_id=picking.id)

        return picking

    def update_picking(
        self,
        quant_ids=None,
        force_validate=False,
        validate=False,
        create_backorder=False,
        location_dest_id=None,
        location_dest_name=None,
        location_dest_barcode=None,
        result_package_name=None,
        package_name=None,
        move_parent_package=False,
        parent_package_packaging_id=None,
        product_ids=None,
        picking_info=None,
        validate_real_time=None,
        location_id=None,
        **kwargs
    ):
        """Update/mutate the stock picking in self

        @param quant_ids: Array (int)
            An array of the quants ID to add to the stock.picking
        @param (optional) force_validate: Boolean
            Forces the transfer to be completed. Depends on parameters
        @param (optional) validate: Boolean
            Validate the transfer unless there are move lines todo, in
            that case it will raise an error.
        @param (optional) create_backorder: Boolean
            When true, allows to validate a transfer with move lines todo
            by creating a backorder.
        @param (optional) location_dest_id: int
            ID of the location where the stock is going to be moved to
        @param (optional) location_dest_name: string
            Name of the location where the stock is going to be moved to
        @param (optional) location_dest_barcode: string
            barcode of the location where the stock is going to be moved to
        @param (optional) result_package_name: string
            If it corresponds to an existing package/pallet that is not
            in an other location, we will set it to the `result_package_id`
            of the operations of the picking (i.e. transfer)
            If the target storage format of the picking type is pallet of
            packages it will set `u_result_parent_package_id` unless
            result_parent_package_name is set, in such case it will be set
            as `result_package_id`.
        @param (optional) result_parent_package_name: string
            name of the package to be set as `u_result_parent_package_id`
        @param (optional) move_parent_package: Boolean
            Used in pallets/nested packages, to maintain the move of the entire pallet.
            Defaults to False
        @param (optional) parent_package_packaging_id: int
            ID of the packaging record (product.packaging)
            to be applied to the parent package
        @param package_name: string
            Name of the package of the picking to be marked as done
        @param product_ids: Array of dictionaries
            An array with the products information to be marked as done,
            where each dictionary contains: barcode, qty and
            lot numbers if needed
        @param picking_info: dictionary
            Generic picking information to update the stock picking with
        @param (optional) validate_real_time: Boolean
            Used to specify if the update should be should be processed
            immediately or on confirmation of the picking.
        @param (optional) location_id: int
            Used when validating products from a location.
        """
        Location = self.env["stock.location"]
        Package = self.env["stock.quant.package"]

        self.assert_valid_state()

        user = self.env.user

        # If transport data has been sent in kwargs, add it to picking_info
        if "u_transport_id" in kwargs:
            # retrieve transport info changes, and then remove them from kwargs
            transport_id = kwargs.pop("u_transport_id")
            # if picking_info already exists update with trailer info
            if picking_info is None:
                picking_info = transport_id
            else:
                picking_info.update(transport_id)

        if "cancel" in kwargs:
            cancel = kwargs.pop("cancel")

            if cancel:
                self.action_cancel()

        if validate_real_time is None:
            validate_real_time = self.picking_type_id.u_validate_real_time

        if "expected_package_names" in kwargs:
            self = self.with_context(expected_package_names=kwargs.pop("expected_package_names"))
            if product_ids:
                # product_ids name kept for consistency
                self = self.with_context(product_ids=product_ids)

        values = {}

        # Updates stock picking with generic picking info
        if picking_info:
            self.write(picking_info)

        if quant_ids:
            # Create extra stock.moves to the picking
            self._create_moves_from_quants(
                quant_ids, confirm=True, assign=True, result_package=result_package_name
            )
            # when adding only do this?
            return True

        location_dest = location_dest_id or location_dest_barcode or location_dest_name
        if location_dest:
            values["location_dest"] = location_dest

        if result_package_name:
            values["result_package"] = result_package_name

        if "result_parent_package_name" in kwargs:
            values["result_parent_package"] = kwargs.pop("result_parent_package_name")

        if not move_parent_package:
            # not needed yet, move it outside udes_stock
            # when false remove parent_id of the result_package_id ??
            # picking.move_line_ids.mapped('result_package_id').write({'package_id': False})
            pass

        # get all the stock.move.lines
        move_lines = self.move_line_ids

        if package_name:
            # a package is being marked as done
            values["package"] = package_name
            package = Package.get_package(package_name)
            move_lines = move_lines.get_package_move_lines(package)
            if not product_ids and not self.picking_type_id.u_user_scans == "product":
                # a full package is being validated
                # check if all parts have been reserved
                package.assert_reserved_full_package(move_lines)

        if product_ids:
            values["product_ids"] = product_ids
            # when updating products we might want
            # to filter by location
            if location_id:
                location = Location.get_location(location_id)
                move_lines = move_lines.filtered(lambda ml: ml.location_id == location)

        picking = self
        if package_name or product_ids or force_validate:
            # mark_as_done the stock.move.lines
            mls_done = move_lines.mark_as_done(**values)

            if validate_real_time:
                picking = self._real_time_update(mls_done)
                validate = True
        elif location_dest:
            # update destination location for move lines
            if result_package_name:
                move_lines = move_lines.filtered(
                    lambda ml: ml.result_package_id.name == result_package_name
                )

            loc_dest_instance = Location.get_location(location_dest)
            if not self.is_valid_location_dest_id(loc_dest_instance):
                raise ValidationError(
                    _(
                        "The location '%s' is not a child of the picking destination "
                        "location '%s'" % (loc_dest_instance.name, self.location_dest_id.name)
                    )
                )

            move_lines_done = move_lines.get_lines_done()
            move_lines_done.write({"location_dest_id": loc_dest_instance.id})

        if parent_package_packaging_id:
            picking.move_line_ids.mapped("u_result_parent_package_id").write(
                {"packaging_id": parent_package_packaging_id}
            )

        if force_validate or validate:

            # Create a backorder for all scanned lines of current user and let existing
            # order with remaining lines in case there is at least one more line to be done
            user_process_partial = self.picking_type_id.u_user_process_partial
            if picking.move_line_ids.get_lines_todo() and user_process_partial:
                mls_done_by_user = move_lines.filtered(
                    lambda ml: ml.qty_done > 0 and ml.u_done_by == user
                )
                picking = self._real_time_update(mls_done_by_user)

            if picking.move_line_ids.get_lines_todo() and not create_backorder:
                raise ValidationError(
                    _("Cannot validate transfer because there" " are move lines todo")
                )
            # by default action_done will backorder the stock.move.lines todo
            # validate stock.picking
            with self.statistics() as stats:
                picking.sudo().with_context(tracking_disable=True).action_done()
            _logger.info(
                "%s (update_picking) action_done in %.2fs, %d queries",
                picking,
                stats.elapsed,
                stats.count,
            )

    def _requires_backorder(self, mls):
        """
        Checks if a backorder is required by checking if all moves within a picking are present
        in the movelines. Cancelled moves are not considered.
        """
        mls_moves = mls.mapped("move_id")
        
        for move in self.move_lines.filtered(lambda mv: mv.state != "cancel"):
            if (
                move not in mls_moves
                or not move.move_line_ids == mls.filtered(lambda ml: ml.move_id == move)
                or move.move_orig_ids.filtered(lambda mv: mv.state not in ("done", "cancel"))
            ):
                return True
        return False

    def _backorder_movelines(self, mls=None, dest_picking=None):
        """Creates a backorder picking from self (expects a singleton)
        and a subset of stock.move.lines are then moved into it.
        The moves from which the move lines have been transferred
        will be split if needed.
        Completed move lines will be selected unless mls is set.
        Optionally receive a picking into where the mls will be moved.
        """
        Move = self.env["stock.move"]
        # Based on back order creation in stock_move._action_done
        self.ensure_one()

        # Select completed move lines when not passed
        if mls is None:
            mls = self.move_line_ids.filtered(lambda x: x.qty_done > 0)

        # Check the intersection of mls and move lines in picking to ensure
        # there are relevant move lines
        if not (mls & self.move_line_ids):
            raise ValidationError(
                _("There are no move lines within picking %s to backorder" % self.name)
            )

        new_moves = Move.browse()
        for current_move in mls.mapped("move_id"):
            current_mls = mls.filtered(lambda x: x.move_id == current_move)
            new_moves |= current_move.split_out_move_lines(current_mls)

        if dest_picking is None:
            # Create picking for selected move lines
            dest_picking = self._create_new_picking(name="/", backorder_id=self.id)

        new_moves.write({"picking_id": dest_picking.id})
        new_moves.mapped("move_line_ids").write({"picking_id": dest_picking.id})

        return dest_picking

    def _real_time_update(self, mls):
        """Checks to see if the transfer of the move_lines would leave the
        stock.move empty if so it returns self else it returns a
        backorder comprising of the stock.move.lines provided
        """
        if not self._requires_backorder(mls):
            return self

        rt_picking = self._backorder_movelines(mls)
        return rt_picking

    def get_pickings(
        self,
        origin=None,
        package_name=None,
        states=None,
        picking_type_ids=None,
        allops=None,
        location_id=None,
        product_id=None,
        backorder_id=None,
        result_package_id=None,
        picking_priorities=None,
        picking_ids=None,
        bulky=None,
        batch_id=None,
        extra_domain=None,
    ):

        """Search for pickings by various criteria

        @param (optional) origin
            Search for stock.picking records based on the origin
            field. Needs to be a complete match.

        @param (optional) package_name
            Search of stock.pickings associated with a specific
            package_name (exact match).

        @param (optional) product_id
            If it is set then location_id must also be set and stock.pickings
            are found using both of those values (states is optional).

        @param (optional) location_id
            If it is set then only internal transfers acting on that
            location are considered. In all cases, if states is set
            then only pickings in those states are considered.

        @param (optional) backorder_id
            Id of the backorder picking. If present, pickings are found
            by backorder_id and states.

        (IGNORE FOR NOW) @param (optional) allops: Boolean.
            If True, all pack operations are included. If False, only
            pack operations that are for the pallet identified by param
            pallet (and it's sub-packages) are included.
            Defaults to True.

        @param (optional) states
            A List of strings that are states for pickings. If present
            only pickings in the states present in the list are
            returned.

        @param (optional) result_package_id
            If an id is supplied all pickings that are registered to
            this package id will be returned. This can also be used
            in conjunction with the states parameter

        @param (optional) picking_priorities
            When supplied all pickings of set priorities and states
            will be searched and returned

        @param (optional) picking_ids
            When supplied pickings of the supplied picking ids will
            be searched and returned. If used in conjunction with
            priorities then only those pickings of those ids will be
            returned.

        @param (optional) bulky: Boolean
            This is used in conjunction with the picking_priorities
            parameter to return pickings that have bulky items
            TODO: this needs to be in a new module and extend this function
                for instance adding the extra criteria to extra_domain paramater

        @param (optional) picking_type_ids: Array (int)
            If it is set the pickings returned will be only from the picking types in the array.

        TODO: bulky
        """
        Picking = self.env["stock.picking"]
        Package = self.env["stock.quant.package"]
        Users = self.env["res.users"]

        order = None

        warehouse = Users.get_user_warehouse()
        if picking_type_ids is None:
            picking_type_ids = warehouse.get_picking_types().ids

        domain = []

        if self:
            domain = [("id", "in", self.mapped("id"))]
        elif origin:
            domain = [("origin", "=", origin)]
        elif backorder_id:
            domain = [("backorder_id", "=", backorder_id)]
        elif result_package_id:
            domain = [("move_line_ids.result_package_id", "=", result_package_id)]
        elif product_id:
            if not location_id:
                raise ValidationError(_("Please supply a location_id"))
            domain = [
                ("move_line_ids.product_id", "=", product_id),
                ("move_line_ids.location_id", "=", location_id),
            ]
        elif package_name:
            package = Package.get_package(package_name, no_results=True)
            if not package:
                return Picking.browse()
            domain = self._get_package_search_domain(package)
        elif picking_priorities:
            domain = [
                ("priority", "in", picking_priorities),
                ("picking_type_id", "=", warehouse.pick_type_id.id),
                ("batch_id", "=", False),
            ]
            if picking_ids is not None:
                domain.append(("id", "in", picking_ids))
            order = "priority desc, scheduled_date, id"
            # TODO: add bulky field
            # if bulky is not None:
            #    domain.append(('u_contains_bulky', '=', bulky))
        elif picking_ids:
            domain = [("id", "in", picking_ids)]
        elif location_id:
            warehouse = Users.get_user_warehouse()
            domain = [
                ("location_id", "=", location_id),
                ("picking_type_id", "=", warehouse.int_type_id.id),
            ]
        elif batch_id is not None:
            domain = [("batch_id", "=", batch_id)]

        if states:
            domain.append(("state", "in", states))

        # add the picking type ids to the domain
        domain.append(("picking_type_id", "in", picking_type_ids))

        # add extra domain if there is any
        if extra_domain:
            domain.extend(extra_domain)

        pickings = Picking.search(domain, order=order)

        return pickings

    def batch_to_user(self, user):
        """Throws error if picking is batched to another user
        or if user already has a batch
        creates batch and adds picking to it otherwise
        """

        PickingBatch = self.env["stock.picking.batch"]

        if self.batch_id:
            if self.batch_id.user_id == user:
                return True
            else:
                if not self.batch_id.user_id:
                    raise ValidationError(
                        _("Picking %s is already in an unassigned batch") % self.name
                    )
                else:
                    raise ValidationError(
                        _("Picking %s is in a batch owned by another user: %s")
                        % (self.name, self.batch_id.user_id.name)
                    )

        if PickingBatch.get_user_batches():
            raise ValidationError(_("You (%s) already have a batch in progess") % user.name)

        if not self.batch_id:
            batch = PickingBatch.create({"user_id": user.id, "u_ephemeral": True})
            self.batch_id = batch.id
            batch.mark_as_todo()

    def _get_package_search_domain(self, package):
        """Generate the domain for searching pickings of a package"""
        return [
            "|",
            ("move_line_ids.package_id", "child_of", package.id),
            "|",
            ("move_line_ids.result_package_id", "child_of", package.id),
            ("move_line_ids.u_result_parent_package_id", "=", package.id),
        ]

    def _prepare_info(self, priorities=None, fields_to_fetch=None):
        """
        Prepares the following info of the picking in self:
        - id: int
        - name: string
        - priority: int
        - backorder_id: int
        - priority_name: string
        - origin: string
        - location_dest_id: int
        - picking_type_id: int
        - move_lines: [{stock.move}]
        - state: string
        - u_pending: boolean (only if picking type does not handle partials)
        - u_original_picking_id

        @param (optional) priorities
            Dictionary of priority_id:priority_name
        """
        self.ensure_one()
        priority_name = self._get_priority_name()

        # @todo: (ale) move this out of the method as it's static code
        info = {
            "id": lambda p: p.id,
            "name": lambda p: p.name,
            "priority": lambda p: p.priority,
            "backorder_id": lambda p: p.backorder_id.id,
            "priority_name": lambda p: priority_name,
            "origin": lambda p: p.origin,
            "state": lambda p: p.state,
            "location_dest_id": lambda p: p.location_dest_id.id,
            "picking_type_id": lambda p: p.picking_type_id.id,
            "moves_lines": lambda p: p.move_lines.get_info(),
            "picking_guidance": lambda p: p.get_picking_guidance(),
            "u_original_picking_id": lambda p: p.u_original_picking_id,
        }

        # u_pending only included if we don't handle partials, otherwise field is irrelevant.
        if self.can_handle_partials() is False:
            info["u_pending"] = lambda p: p.u_pending

        if not fields_to_fetch:
            fields_to_fetch = info.keys()

        if self.picking_type_id.u_reserve_pallet_per_picking:
            info["u_reserved_pallet"] = lambda p: p.u_reserved_pallet

        return {key: value(self) for key, value in info.items() if key in fields_to_fetch}

    def _get_priority_dict(self):
        return OrderedDict(self._fields["priority"].selection)

    def _get_priority_name(self):
        return self._get_priority_dict().get(self.priority)

    def get_picking_guidance(self):
        """ Return dict of guidance info to aid user when picking """
        return {"Priorities": self._get_priority_name()}

    def get_info(self, **kwargs):
        """Return a list with the information of each picking in self."""
        # create a dict of priority_id:priority_name to avoid
        # to do it for each picking
        priorities = self._get_priority_dict()
        res = []
        for picking in self:
            res.append(picking._prepare_info(priorities, **kwargs))

        return res

    def get_result_packages_names(self):
        """Return a list of the names of the parent packages"""
        move_lines = self.mapped("move_line_ids")
        packages = move_lines.mapped(
            lambda ml: ml.u_result_parent_package_id or ml.result_package_id
        )
        return packages.mapped("name")

    def _priorities_has_ready_pickings(self, priorities, picking_type_id):
        domain = [
            ("picking_type_id", "=", picking_type_id),
            ("priority", "in", priorities),
            ("state", "=", "assigned"),
        ]
        return self.search_count(domain) >= 1

    @api.model
    def _get_priority_groups(self):
        return list(common.PRIORITY_GROUPS.values())

    @api.model
    def get_priorities(self, picking_type_id=None):
        """Return a list of dicts containing the priorities of the
        all defined priority groups, in the following format:
            [
                {
                    'name': 'Picking',
                    'priorities': [
                        OrderedDict([('id', '2'), ('name', 'Urgent')]),
                        OrderedDict([('id', '1'), ('name', 'Normal')])
                    ]
                },
                {
                    ...
                },
                ...
            ]
        """

        if picking_type_id is None:
            return self._get_priority_groups()

        groups_with_pickings = []
        for group in self._get_priority_groups():
            priorities = [p["id"] for p in group["priorities"]]
            if self._priorities_has_ready_pickings(priorities, picking_type_id):
                groups_with_pickings.append(group)
        return groups_with_pickings

    @api.multi
    def get_move_lines_done(self):
        """ Return the recordset of move lines done. """
        move_lines = self.mapped("move_line_ids")

        return move_lines.filtered(lambda o: o.qty_done > 0)

    def _get_child_dest_locations(self, domains=None, limit=None):
        """Return the child locations of the instance dest location.
        Extra domains are added to the child locations search query,
        when specified.
        Expects a singleton instance.
        """
        Location = self.env["stock.location"]
        self.ensure_one()

        if domains is None:
            domains = []

        domains.append(("id", "child_of", self.location_dest_id.id))
        return Location.search(domains, limit=limit)

    def is_valid_location_dest_id(self, location=None, location_ref=None):
        """Whether the specified location or location reference
        (i.e. ID, name or barcode) is a valid putaway location
        for the picking.
        Expects a singleton instance.

        Returns a boolean indicating the validity check outcome.
        """
        Location = self.env["stock.location"]
        self.ensure_one()

        if not location and not location_ref:
            raise ValidationError("Must specify a location or ref")

        dest_locations = None

        if location is not None:
            dest_locations = location
        else:
            dest_locations = Location.get_location(location_ref)

        if not dest_locations:
            raise ValidationError(_("The specified location is unknown."))

        valid_locations = self._get_child_dest_locations([("id", "in", dest_locations.ids)])

        return valid_locations.exists()

    @api.multi
    def open_stock_picking_form_view(self):
        self.ensure_one()
        view_id = self.env.ref("stock.view_picking_form").id
        return {
            "name": _("Internal Transfer"),
            "type": "ir.actions.act_window",
            "view_type": "form",
            "view_mode": "form",
            "res_model": "stock.picking",
            "views": [(view_id, "form")],
            "view_id": view_id,
            "res_id": self.id,
            "context": dict(self.env.context),
        }

    @api.multi
    def _refine_picking(self, text=None):
        """
        Unreserve the move lines of all pickings of the instance
        recordset.
        If the text argument is specified, it will be shown in the
        Odoo UI within each picking context.
        """
        for pick in self:
            if text:
                pick.message_post(body=text)

            pick.do_unreserve()

    def action_confirm(self):
        """
        Override action_confirm to create procurement groups if needed
        """
        for pick in self.filtered(
            lambda p: p.picking_type_id.u_create_procurement_group and not p.group_id
        ):
            pick._create_own_procurement_group()
        res = super(StockPicking, self).action_confirm()

        if self:
            self.unlink_empty()
        return res

    def find_empty_pickings_to_unlink(self):
        """Finds empty pickings to unlink in self, when it is set, otherwise
        searches for any empty picking.

        Filter out of the generic search when the picking type does not
        have auto unlink empty enabled.
        """
        PickingType = self.env["stock.picking.type"]

        # All pickings to be unlinked should not be marked
        domain = [("u_mark", "=", False)]
        if self:
            domain.append(("id", "in", self.ids))
        else:
            pts = PickingType.search([("u_auto_unlink_empty", "=", False)])
            if pts:
                domain.append(("picking_type_id", "not in", pts.ids))
        while True:
            records = self.search(domain, limit=1000)
            if not records:
                break
            yield records

    def unlink_empty(self):
        """
        Delete pickings in self that are empty, locked and marked=False.
        If self is empty, find and delete any picking with the previous
        criterion.
        This is to prevent us leaving junk data behind when refactoring
        """
        records = self.browse()
        for to_unlink in self.find_empty_pickings_to_unlink():
            records |= to_unlink
            _logger.info("Unlinking empty pickings %s", to_unlink)
            moves = to_unlink.mapped("move_lines")
            move_lines = to_unlink.mapped("move_line_ids")
            non_empty_pickings = moves.mapped("picking_id") | move_lines.mapped("picking_id")
            if non_empty_pickings:
                raise ValidationError(
                    _("Trying to unlink non empty pickings: " "%s" % non_empty_pickings.ids)
                )
            to_unlink.unlink()

        return self - records

    def _check_entire_pack(self):
        """
        Override to avoid values at result_package_id when
        user scans products/avoid _check_entire_pack of products
        """
        pickings = self.filtered(
            lambda p: p.picking_type_id.u_user_scans != "product"
            and p.picking_type_id.u_target_storage_format != "product"
        )
        super(StockPicking, pickings)._check_entire_pack()

    def _set_u_result_parent_package_id(self):
        """
        Override function to avoid setting result_package_id when
        the storage format is product/package/pallet of products
        """
        pallet_package_pickings = self.filtered(
            lambda p: (
                p.picking_type_id.u_target_storage_format
                not in ("product", "pallet_products", "package")
            )
            and not (
                p.picking_type_id.u_target_storage_format == "pallet_packages"
                and p.picking_type_id.u_user_scans == "package"
            )
        )
        super(StockPicking, pallet_package_pickings)._set_u_result_parent_package_id()

    def _should_reserve_full_packages(self):
        """Method to determine if picking should reserve entire packages"""
        self.ensure_one()
        return self.picking_type_id.u_reserve_as_packages

    def _reserve_full_packages(self):
        """
        If the picking type of the picking in self has full package
        reservation enabled, partially reserved packages are
        completed.
        """
        Quant = self.env["stock.quant"]
        MoveLine = self.env["stock.move.line"]

        # do not reserve full packages when bypass_reserve_full packages
        # is set in the context as True
        if not self.env.context.get("bypass_reserve_full_packages"):
            for picking in self.filtered(lambda p: p._should_reserve_full_packages()):
                all_quants = Quant.browse()
                remaining_qtys = defaultdict(int)

                # get all packages
                packages = picking.mapped("move_line_ids.package_id")
                for package in packages:
                    move_lines = picking.mapped("move_line_ids").filtered(
                        lambda ml: ml.package_id == package
                    )
                    # TODO: merge with assert_reserved_full_package
                    pack_products = frozenset(package._get_all_products_quantities().items())
                    mls_products = frozenset(move_lines._get_all_products_quantities().items())
                    if pack_products != mls_products:
                        # move_lines do not match the quants
                        pack_mls = MoveLine.search(
                            [
                                ("package_id", "child_of", package.id),
                                ("state", "not in", ["done", "cancel"]),
                            ]
                        )
                        other_pickings = pack_mls.mapped("picking_id") - picking
                        if other_pickings:
                            raise ValidationError(
                                _("The package is reserved in other pickings: %s")
                                % ",".join(other_pickings.mapped("name"))
                            )

                        quants = package._get_contained_quants()
                        all_quants |= quants
                        for product, qty in quants.group_quantity_by_product(
                            only_available=True
                        ).items():
                            remaining_qtys[product] += qty
                if remaining_qtys:
                    # Context variables:
                    # - filter the quants used in _create_moves() to be
                    # the ones of the packages to be completed
                    # - add bypass_reserve_full_packages at the context
                    # to avoid to be called again inside _create_moves()
                    picking.with_context(
                        bypass_reserve_full_packages=True, quant_ids=all_quants.ids
                    )._create_moves(
                        remaining_qtys,
                        values={"priority": picking.priority},
                        confirm=True,
                        assign=True,
                    )

    def _create_own_procurement_group(self):
        """Create a procurement group for self with the same name as self."""
        self.ensure_one()
        group = self.env["procurement.group"].create({"name": self.name})
        self.move_lines.write({"group_id": group.id})

    def _update_move_lines_and_log_swap(
        self, move_lines, packs, other_pack, fields_to_consider=None, fields_to_update=None
    ):
        """Set the package ids of the specified move lines to the one
        of the other package and signal the package swap by posting
        a message to the picking instance of the first move line.

        Assumes that both packages are singletons and that the
        specified move lines are a non empty recordset.
        """
        values = {}
        values.update(self._swap_moveline_info_at_pack_level(other_pack, values))
        move_lines.with_context(bypass_reservation_update=True).write(values)
        self._swap_moveline_info_below_pack_level(
            move_lines, other_pack, fields_to_consider, fields_to_update
        )
        msg_args = (", ".join(packs.mapped("name")), other_pack.name)
        msg = _("Package %s swapped for package %s.") % msg_args
        move_lines.mapped("picking_id").message_post(body=msg)
        _logger.info(msg)

    def _swap_moveline_info_at_pack_level(self, pack, values):
        values.update({"package_id": pack.id, "result_package_id": pack.id})
        return values

    def _swap_moveline_info_below_pack_level(
        self, move_lines_to_use, pack, fields_to_consider=None, fields_to_update=None
    ):
        """
        Swapping of fields which are more granular than pack level - from quant to move_lines
        specifically, the lots.
        """

        if fields_to_update is None:
            fields_to_update = {"lot_id"}
        if fields_to_consider is None:
            fields_to_consider = {"product_id"}

        grouping_fields = fields_to_consider.union(fields_to_update)

        MoveLine = self.env["stock.move.line"]
        move_lines_to_skip = MoveLine.browse()
        for fields, quant_ids in (
            pack.mapped("quant_ids")
            .sorted(key=lambda quant: [getattr(quant, field) for field in grouping_fields])
            .groupby(
                lambda quant: {
                    field: getattr(quant, field)
                    for field in fields_to_consider.union(fields_to_update)
                },
                sort=False,
            )
        ):
            move_lines_to_write = MoveLine.browse()
            product_lot_qty_in_quants = sum(quant_ids.mapped("quantity"))
            product_move_lines = move_lines_to_use.filtered(
                lambda move_line: all(
                    getattr(move_line, field) == fields[field] for field in fields_to_consider
                )
                and move_line not in move_lines_to_skip
            )
            result_of_move_line_fulfillment = self._quantity_matching_of_move_lines(
                product_move_lines, product_lot_qty_in_quants
            )
            move_lines_to_write |= result_of_move_line_fulfillment["used_move_lines"]
            if result_of_move_line_fulfillment["new_move_line"]:
                move_lines_to_use |= result_of_move_line_fulfillment["new_move_line"]
            move_lines_to_skip |= move_lines_to_write
            move_lines_to_write.with_context(bypass_reservation_update=True).write(
                {field: fields[field].id or fields[field] for field in fields_to_update}
            )

    def _quantity_matching_of_move_lines(self, move_lines_to_fulfill, quantity_to_fulfill):
        """Helper function to abstract tuple returned from move_line quantity fulfillment call"""
        result_of_move_line_fulfillment = move_lines_to_fulfill.move_lines_for_qty(
            quantity_to_fulfill
        )
        return {
            "used_move_lines": result_of_move_line_fulfillment[0],
            "new_move_line": result_of_move_line_fulfillment[1],
            "unfulfilled_requested_quantity": result_of_move_line_fulfillment[2],
        }

    def _swap_quant_fields(self, packs, pack_prime, fields_to_swap=None):
        """Method to swap arbitrary quant fields between different packs
        the fields_to_swap are the string values of the fields which
        we are to swap between packs
        """

        if fields_to_swap is None:
            return False

        Quant = self.env["stock.quant"]

        new_quants = Quant.browse()
        quants_to_skip = Quant.browse()

        def _split_quant_swap_field(smaller_quant, larger_quant, fields_to_swap):
            """
            Helper function to split quants and swap fields, while retaining the total quantity
            for given fields.

            During splitting, the larger_quant will be the remainder of the splitting, that
            is to say it will be the original quant with updated values, this means that
            during the swapping of fields we retain the total quantity for each fields.

            i.e. smaller_quant = 3 x product_1 with field_X_val_1
                 larger_quant = 5 x product_1 with field_X_val_2

                 Applying split will return a new quant with 3 x product_1
                 so then we have:
                 smaller_quant = 3 x product_1 with field_X_val_1
                 larger_quant = 2 x product_1 with field_X_val_2
                 quant_split = 3 x product_1 with field_X_val_2

                 We then switch the fields to get:
                 smaller_quant = 3 x product_1 with field_X_val_2
                 larger_quant = 2 x product_1 with field_X_val_2
                 quant_split = 3 x product_1 with field_X_val_1
            """
            quant_split = larger_quant._split(smaller_quant.quantity)
            for field in fields_to_swap:
                quant_split[field] = smaller_quant[field]
                smaller_quant[field] = larger_quant[field]
            return quant_split

        packs_quants = packs.mapped("quant_ids")
        pack_prime_quants = pack_prime.quant_ids

        # Cycle through quants in candidate packs used for swapping
        for packs_quant in packs_quants:
            # Filter the quants in the original pack being swapped to match the product in packs quant
            pack_prime_product_quants = pack_prime_quants.filtered(
                lambda q: q.product_id == packs_quant.product_id and q not in quants_to_skip
            )
            # Cycle through the quants for product within the pack being swapped (as this may not be singular for a field)
            for pack_prime_quant in pack_prime_product_quants:
                # Check that the quants haven't already been depleted elsewhere
                if pack_prime_quant.quantity and packs_quant.quantity:
                    # In the case of matching quantity, simply swap the field values
                    if packs_quant.quantity == pack_prime_quant.quantity:
                        # remove the quant from the usable set
                        for field in fields_to_swap:
                            holding_field = packs_quant[field]
                            packs_quant[field] = pack_prime_quant[field]
                            pack_prime_quant[field] = holding_field
                        quants_to_skip |= pack_prime_quant
                        break

                    # In other cases, split the larger by the smaller
                    elif packs_quant.quantity < pack_prime_quant.quantity:
                        new_quants |= _split_quant_swap_field(
                            packs_quant, pack_prime_quant, fields_to_swap
                        )
                        # As the quant from packs in this case has been fulfilled, break to the original pack quant loop
                        break

                    else:  # packs_quant.quantity > pack_prime_quant.quantity case
                        new_quants |= _split_quant_swap_field(
                            pack_prime_quant, packs_quant, fields_to_swap
                        )
                        quants_to_skip |= pack_prime_quant

        # Merge and delete where appropriate
        all_quants = packs_quants | pack_prime_quants | new_quants
        self._quant_cleanup(all_quants)

    def _quant_cleanup(self, quants):
        location_ids = quants.mapped("location_id").ids
        quants._merge_quants(location_ids)
        self.env.clear()

    def _swap_adjust_reserved_quantity(self, quants, prod_quantities):
        """Adjust the reserved quantity by removing `prod_quantities` from the
        reserved_quantity of the quants.

        Returns the quants which haven't been touched.
        """
        for prod, quantity in prod_quantities.items():
            for q in quants.filtered(lambda q: q.product_id == prod):
                if quantity == 0:
                    break
                qty = min([quantity, q.quantity])
                q.sudo().write({"reserved_quantity": qty})
                quantity -= qty
                quants -= q
        return quants

    def _get_adjusted_quantities(self, pack_mls):
        """Get the total `reserved_quantity` for each product in the quants
        the total `product_qty` of the move lines is then subtracted.

        Note: we shouldn't get negative values as a move line must have
        `product_qty` <= its quants `reserved_quantity`
        """
        adjusted_quantities = {}

        for prod, quants in pack_mls.get_quants().groupby("product_id"):
            reserved_qty = sum(quants.mapped("reserved_quantity"))
            mls_prod_qty = sum(
                pack_mls.filtered(lambda ml: ml.product_id == prod).mapped("product_qty")
            )
            adjusted_qty = reserved_qty - mls_prod_qty
            # skip 0 quantities as we can leave them untouched and do them at
            # the end
            if adjusted_qty != 0:
                adjusted_quantities[prod] = adjusted_qty

        return adjusted_quantities

    def _swap_unreserved(self, scanned_package, expected_package, exp_pack_mls):
        """We know that scanned_pack_mls is empty; we should now
        1) unreserve the `product_quantity` of `exp_pack_mls` from its quants
        2) reserve quants of the scanned package
        3) change the package ids of the expected move lines
        """
        for _pack, pack_mls in exp_pack_mls.groupby("package_id"):
            untouched_quants = self._swap_adjust_reserved_quantity(
                pack_mls.get_quants(), self._get_adjusted_quantities(pack_mls)
            )
            untouched_quants.sudo().write({"reserved_quantity": 0})

        self._swap_adjust_reserved_quantity(
            scanned_package._get_contained_quants(), exp_pack_mls._get_all_products_quantities()
        )
        self._handle_move_line_swap(exp_pack_mls, expected_package, scanned_package)

    def _swap_entire_package(
        self, scanned_package, expected_package, scanned_pack_mls, exp_pack_mls
    ):
        """ Performs the swap. """
        expected_package.ensure_one()

        if scanned_pack_mls and exp_pack_mls:
            # Both packages are in move lines; we simply change
            # the package ids of both scanned and expected move lines
            self._handle_move_line_swap(
                scanned_pack_mls, scanned_package, expected_package, exp_pack_mls
            )
        elif exp_pack_mls:
            # No scanned_pack_mls so scanned_pack is not reserved
            self._swap_unreserved(scanned_package, expected_package, exp_pack_mls)
        else:
            raise AssertionError("No expected pack move lines")

        return exp_pack_mls

    def _handle_move_line_swap(
        self,
        move_lines,
        packs,
        other_pack,
        additional_mls=False,
        fields_to_consider=None,
        fields_to_update=None,
    ):
        if additional_mls:
            # Both packages are in move lines; we simply change
            # the package ids of both scanned and expected move lines
            self._update_move_lines_and_log_swap(
                move_lines, packs, other_pack, fields_to_consider, fields_to_update
            )
            self._update_move_lines_and_log_swap(
                additional_mls, other_pack, packs, fields_to_consider, fields_to_update
            )

        else:
            self._update_move_lines_and_log_swap(
                move_lines, packs, other_pack, fields_to_consider, fields_to_update
            )

    def _swap_package_contents(
        self, scanned_package, expected_packages, scanned_pack_mls, exp_pack_mls
    ):

        if scanned_pack_mls and exp_pack_mls and scanned_pack_mls & exp_pack_mls:
            # Remove move lines in exp_pack_mls to see if we can
            # treat the scanned pack as unreserved
            scanned_pack_mls -= exp_pack_mls

        if scanned_pack_mls and exp_pack_mls:
            # Swap expected packs for scanned pack
            self._handle_move_line_swap(exp_pack_mls, expected_packages, scanned_package)

            candidate_scan_mls = scanned_pack_mls[:]
            for pack in expected_packages:
                scan_mls, excess_ml = pack.mls_can_fulfil(candidate_scan_mls)
                candidate_scan_mls -= scan_mls
                candidate_scan_mls += excess_ml
                self._handle_move_line_swap(scan_mls, scanned_package, pack)
        elif exp_pack_mls:
            self._swap_unreserved(scanned_package, expected_packages, exp_pack_mls)
        else:
            raise AssertionError("No expected pack move lines")

        return exp_pack_mls

    def _get_mls_for_entire_package_swap(self, scanned_package, expected_package):
        """check if an entire package can be swapped"""
        expected_package.ensure_one()

        if not scanned_package.has_same_content(expected_package):
            raise ValidationError(
                _("The contents of %s does not match what you have been asked to pick.")
                % scanned_package.name
            )

        if not scanned_package.is_reserved():
            return None

        scanned_pack_mls = scanned_package.find_move_lines()

        if not scanned_pack_mls:
            raise ValidationError(
                _("Package %s is reserved for a picking type you are not " "allowed to work with.")
                % scanned_package.name
            )

        if scanned_pack_mls.filtered(lambda ml: ml.qty_done > 0):
            raise ValidationError(
                _("Package %s has move lines already done.") % scanned_package.name
            )

        return scanned_pack_mls

    def _get_mls_for_package_content_swap(self, scanned_package, expected_packages, mls):
        """Returns mls which the package can fulfil. If the product_qty of
        the mls is larger than in the package (i.e. in self) the mls will be
        split.

        Note: Move lines will be split to maximise the amount fulfilled by the
        scanned package.
        """

        # Get move_lines which the scanned package can fulfil
        # Note this will split the move lines to maximize scanned package use
        mls, _excess_ml = scanned_package.mls_can_fulfil(mls)

        if not mls:
            msg_args = (
                scanned_package.name,
                ", ".join(mls.ids),
                ", ".join(expected_packages.mapped("name")),
            )
            raise ValidationError(
                _(
                    "Package %s can not fulfil the move line(s)(%s) so can not "
                    "swap with package(s) %s"
                )
                % msg_args
            )

        if not scanned_package.is_reserved():
            return None, mls

        scanned_pack_mls = scanned_package.find_move_lines().get_lines_todo()

        if not scanned_pack_mls:
            raise ValidationError(
                _("Package %s is reserved for a picking type you are not " "allowed to work with.")
                % scanned_package.name
            )

        # find pickings which contains scanned_package
        # if any of them are batch (that aren't this one) which in progress
        # Then we can't switch them as we may cause collisions

        # All mls have picking and therefore batch
        current_batch = mls[0].picking_id.batch_id
        batch_check = lambda b: (b.state == "in_progress" and b.id != current_batch.id)
        in_progress_batches = scanned_pack_mls.mapped("picking_id.batch_id").filtered(batch_check)

        if in_progress_batches:
            raise ValidationError(
                _('Package %s is contained within an "in progress" batch.') % scanned_package.name
            )

        return scanned_pack_mls, mls

    def maybe_swap(self, scanned_package, expected_packages):
        """Validate the conditions for performing a swap of the
        specified packages by considering the picking instance
        (expects a singleton picking) and relevant move lines.

        If the user scans (`u_user_scans`) is set to 'product' then the
        contents of the `scanned_package` which match with the
        contents within `expected_packages` will be swapped.

        If the user scans is set to 'package' then `expected_packages`
        should be a singleton and the entire package will be swapped if
        all of the contents match.

        Return the move lines related to the expected package (or a subset
        including splitting), in case a swap is performed, or the ones
        related to the scanned package, if both packages belong to the same
        batch.

        Raise a ValidationError in case packages cannot be found
        in the picking or if the conditions for swapping are not
        met.
        """
        self.ensure_one()

        if not self.picking_type_id.u_allow_swapping_packages:
            raise ValidationError(
                _("Cannot swap packages of picking type '%s'") % self.picking_type_id.name
            )

        exp_pack_mls = self.move_line_ids.get_lines_todo().filtered(
            lambda ml: ml.package_id in expected_packages
        )

        if not exp_pack_mls:
            raise ValidationError(
                _("Expected package(s) cannot be found in picking %s") % self.name
            )

        expected_package_location = expected_packages.mapped("location_id")
        expected_package_location.ensure_one()
        scanned_package.ensure_one()

        if scanned_package.location_id != expected_package_location:
            raise ValidationError(_("Packages are in different locations and cannot be swapped"))

        if self.picking_type_id.u_user_scans == "product":
            scanned_pack_mls, exp_pack_mls = self._get_mls_for_package_content_swap(
                scanned_package, expected_packages, exp_pack_mls
            )
        else:
            scanned_pack_mls = self._get_mls_for_entire_package_swap(
                scanned_package, expected_packages
            )
        if scanned_pack_mls:
            # We know that all the move lines have the same picking id
            mls_picking = scanned_pack_mls[0].picking_id

            if mls_picking.picking_type_id != self.picking_type_id:
                raise ValidationError(
                    _("Packages have different picking types and cannot be " "swapped")
                )

        if self.picking_type_id.u_user_scans == "product":
            return self._swap_package_contents(
                scanned_package, expected_packages, scanned_pack_mls, exp_pack_mls
            )
        else:
            if scanned_pack_mls and self.batch_id and mls_picking.batch_id == self.batch_id:
                # The scanned package and the expected are in
                # the same batch; don't need to be swapped -
                # simply return the move lines of the scanned
                # package
                return scanned_pack_mls

            return self._swap_entire_package(
                scanned_package, expected_packages, scanned_pack_mls, exp_pack_mls
            )

    def is_compatible_package(self, package_name):
        """The package with name package_name is compatible
        with the picking in self if:
        - The package does not exist
        - The package is not in stock
        - The package has not been used in any other picking
        """
        Picking = self.env["stock.picking"]
        Package = self.env["stock.quant.package"]

        self.ensure_one()
        self.assert_valid_state()

        res = True
        pickings = Picking.get_pickings(package_name=package_name)
        if len(pickings) == 0:
            package = Package.get_package(package_name, no_results=True)
            if package and package.quant_ids or package.children_ids:
                # the package exists and it contains stock or other packages
                res = False
        elif len(pickings) > 1 or (len(pickings) == 1 and self != pickings):
            # the package has been used
            res = False

        return res

    @api.model
    def _new_picking_for_group(self, group_key, moves, **kwargs):
        Group = self.env["procurement.group"]

        picking_type = moves.mapped("picking_type_id")
        picking_type.ensure_one()
        src_loc = moves.mapped("location_id")
        dest_loc = moves.mapped("location_dest_id")

        group = Group.get_group(group_identifier=group_key, create=True)

        # Look for an existing picking with the right group
        picking = self.search(
            [
                ("picking_type_id", "=", picking_type.id),
                ("location_id", "=", src_loc.id),
                ("location_dest_id", "=", dest_loc.id),
                ("group_id", "=", group.id),
                # NB: only workable pickings
                ("state", "in", ["assigned", "confirmed", "waiting"]),
            ]
        )

        if not picking:
            # Otherwise reuse the existing picking if all the moves
            # already belong to it and it contains no other moves
            # The picking_type_id, location_id and location_dest_id
            # will match already
            current_picking = moves.mapped("picking_id")
            if len(current_picking) == 1 and current_picking.mapped("move_lines") == moves:
                values = {"group_id": group.id}
                values.update(kwargs)

                current_picking.write(values)
                picking = current_picking

        if not picking or len(picking) > 1:
            # There was no suitable picking to reuse.
            # Create a new picking.
            values = {
                "picking_type_id": picking_type.id,
                "location_id": src_loc.id,
                "location_dest_id": dest_loc.id,
                "group_id": group.id,
            }
            values.update(kwargs)

            picking = self.create(values)

        else:
            # Avoid misleading values for extra fields.
            # If any of the fields in kwargs is set and its value is different
            # than the new one, set the field value to False to avoid misleading
            # values.
            # For instance, picking.origin is 'ASN001' and kwargs contains
            # origin with value 'ASN002', picking.origin is set to False.
            for field, value in kwargs.items():
                current_value = getattr(picking, field, None)
                if isinstance(current_value, models.BaseModel):
                    current_value = current_value.id
                if current_value and current_value != value:
                    setattr(picking, field, False)

        moves.write({"group_id": group.id, "picking_id": picking.id})

        mls = moves.mapped("move_line_ids")
        if mls:
            mls.write({"picking_id": picking.id})
            # After moving move lines check entire packages again just in case
            # some of the move lines are completing packages
            if picking.state != "done":
                picking._check_entire_pack()

        return picking

    #
    ## Suggested locations policies
    #

    def check_policy_for_preprocessing(self, policy):
        """ "Check policy allows pre processing as not all polices
        can be used in this way
        """
        func = getattr(self, "_get_suggested_location_" + policy, None)

        if not hasattr(func, "_allow_preprocess"):
            raise ValidationError(
                _("This policy(%s) is not meant to be used in " "preprocessing") % policy
            )

    def apply_drop_location_policy(self):
        """Apply suggested locations to move lines

        raise ValidationError: if the policy set does not have
                               _allow_preprocess set
        """
        by_pack_or_single = lambda ml: ml.package_id.package_id or ml.package_id or ml.id

        for pick in self:
            self.check_policy_for_preprocessing(pick.picking_type_id.u_drop_location_policy)
            # Group by pallet or package
            for _pack, mls in pick.move_line_ids.groupby(by_pack_or_single):
                locs = pick.get_suggested_locations(mls)
                if locs:
                    mls.write({"location_dest_id": locs[0].id})

    def get_suggested_locations(self, move_line_ids, limit=None, sort=True):
        """Dispatch the configured suggestion location policy to
        retrieve the suggested locations
        """
        result = self.env["stock.location"]

        # WS-MPS: use self.ensure_one() and don't loop (self is used in all but
        # one place), or suggest locations for the move lines of the picking,
        # use picking instead of self inside the loop and ?intersect? result.
        for picking in self:
            policy = picking.picking_type_id.u_drop_location_policy

            if policy:
                if (
                    move_line_ids
                    and picking.picking_type_id.u_drop_location_preprocess
                    and not move_line_ids.any_destination_locations_default()
                ):
                    # The policy has been preprocessed this assumes the
                    # the policy is able to provide a sensible value (this is
                    # not the case for every policy)
                    # Use the preselected value
                    result = self._get_suggested_location_exactly_match_move_line(move_line_ids)

                    # Just to prevent running it twice
                    if not result and policy == "exactly_match_move_line":
                        return result

                # If the pre-selected value is blocked
                if not result:
                    func = getattr(self, "_get_suggested_location_" + policy, None)
                    if func:
                        # The signature of each "get suggested location" method may not
                        # match, so inspect the method and only pass in the expected args
                        expected_args = inspect.getfullargspec(func)[0]
                        keys = ["move_line_ids", "limit", "sort"]
                        local_vars = locals()
                        kwargs = {x: local_vars[x] for x in keys if x in expected_args}
                        result = func(**kwargs)
                        # If result is sorted by the suggest method, don't re-sort it
                        if "sort" in expected_args:
                            sort = False
        if sort:
            return result.sorted(lambda l: l.name)
        else:
            return result

    def get_empty_locations(self, limit=None, sort=True):
        """Returns the recordset of locations that are child of the
        instance dest location, are not blocked, and are empty.
        Expects a singleton instance.
        """
        locations = self._get_child_dest_locations(
            [("u_blocked", "=", False), ("barcode", "!=", False), ("quant_ids", "=", False)],
            limit=limit,
        )
        if sort:
            return locations.sorted(lambda l: l.name)
        else:
            return locations

    def _check_picking_move_lines_suggest_location(self, move_line_ids):
        pick_move_lines = self.mapped("move_line_ids").filtered(lambda ml: ml in move_line_ids)

        if len(pick_move_lines) != len(move_line_ids):
            raise ValidationError(
                _(
                    "Some move lines not found in picking %s to suggest "
                    "drop off locations for them." % self.name
                )
            )

    def _get_suggested_location_exactly_match_move_line(self, move_line_ids):
        self._check_picking_move_lines_suggest_location(move_line_ids)
        location = move_line_ids.mapped("location_dest_id")

        location.ensure_one()

        if location.u_blocked or location.usage == "view":
            return self.env["stock.location"]

        return location

    def _get_suggested_location_empty_location(self, limit=None, sort=False):
        """Get suggested locations by finding locations which are empty."""
        return self.get_empty_locations(limit=limit, sort=sort)

    def _get_suggested_location_by_products(
        self, move_line_ids, limit=None, products=None, sort=False
    ):
        """Get suggested locations by matching product."""
        return self._suggest_locations_products(move_line_ids, products, limit=limit, sort=sort)

    def _get_suggested_location_by_product_lot(self, move_line_ids, products=None):
        """Get suggested locations by matching both product and lot number.

        If used on a product that is not tracked by lot then locations will be suggested
        based on products.
        """
        return self._suggest_locations_products(move_line_ids, products, match_lots=True)

    def _suggest_locations_products(
        self, move_line_ids, products=None, match_lots=False, limit=None, sort=False
    ):
        """Get suggested locations by matching product and optionally lot number.

        If used on a product that is not tracked by lot then locations will be suggested
        based on products.

        Assumption: match_lots not used for goods in process, i.e. based on lots with ids.
        During goods in, lots of quants on movelines won't be committed to the database so won't
        have ids, only names. To allow this to work for goods in the search would have to be on
        the name instead which would be less efficient.

        Limit only limits the number of empty locs to suggest
        """
        Quant = self.env["stock.quant"]

        if not move_line_ids:
            raise ValidationError(_("Cannot determine the suggested location: missing move lines"))

        self._check_picking_move_lines_suggest_location(move_line_ids)

        if products is None:
            products = move_line_ids.mapped("product_id")

        if not products:
            raise ValidationError(_("Products missing to suggest location for."))

        if match_lots:
            if len(products) > 1:
                raise ValidationError(_("Cannot drop different products by lot number."))
            lot_tracking = products.tracking == "lot"
            if lot_tracking:
                lots = move_line_ids.filtered(lambda ml: ml.product_id in products).mapped("lot_id")
                if len(lots) != 1:
                    raise ValidationError(
                        _("Expecting a single lot number " "when dropping by product and lot.")
                    )

        quant_domain = [
            ("product_id", "in", products.ids),
            ("location_id", "child_of", self.location_dest_id.ids),
            ("location_id.u_blocked", "=", False),
            ("location_id.barcode", "!=", False),
        ]
        if match_lots and lot_tracking:
            quant_domain.append(("lot_id", "in", lots.ids))
        suggested_locations = Quant.search(quant_domain).mapped("location_id")

        if not suggested_locations:
            # No drop locations currently used for this product;
            # gather the empty ones
            suggested_locations = self.get_empty_locations(limit=limit, sort=sort)

        return suggested_locations

    def _get_suggested_location_by_packages(self, move_line_ids):
        package = move_line_ids.mapped("package_id")
        package.ensure_one()

        mls = self.mapped("move_line_ids").filtered(lambda ml: ml.package_id == package)

        if not mls:
            raise ValidationError(
                _(
                    "Package %s not found in picking %s in order to suggest "
                    "drop off locations for it." % (package.name, self.name)
                )
            )

        products = mls.mapped("product_id")

        return self._get_suggested_location_by_products(mls, products)

    @allow_preprocess
    def _get_suggested_location_by_height_speed(self, move_line_ids):
        """Get location based on product height and turn over speed categories"""
        Location = self.env["stock.location"]

        height_category = move_line_ids.mapped("product_id.u_height_category_id")
        speed_category = move_line_ids.mapped("product_id.u_speed_category_id")
        default_location = move_line_ids.mapped("picking_id.location_dest_id")

        if not len(height_category) == 1 or not len(speed_category) == 1:
            raise UserError(
                _("Move lines with more than category for height(%s) or " "speed(%s) provided")
                % (height_category.mapped("name"), speed_category.mapped("name"))
            )

        default_location.ensure_one()

        # Get empty locations where height and speed match product
        candidate_locations = Location.search(
            [
                ("location_id", "child_of", default_location.id),
                ("u_blocked", "=", False),
                ("barcode", "!=", False),
                ("u_height_category_id", "in", [height_category.id, False]),
                ("u_speed_category_id", "in", [speed_category.id, False]),
                # TODO(MTC): This should probably be a bit more inteligent perhaps
                # get them all then do a filter for checking if theres space
                ("quant_ids", "=", False),
            ]
        )

        return self._location_not_in_other_move_lines(candidate_locations)

    def _get_suggested_location_by_orderpoint(self, move_line_ids):
        """Same as by product, but the locations are search from order points
        and when there is no suggested location we don't return empty
        locations, since products should only be dropped into locations
        with order points of such products.
        """
        OrderPoint = self.env["stock.warehouse.orderpoint"]

        if not move_line_ids:
            raise ValidationError(_("Cannot determine the suggested location: missing move lines"))

        self._check_picking_move_lines_suggest_location(move_line_ids)

        products = move_line_ids.mapped("product_id")

        orderpoints = OrderPoint.search(
            [
                ("product_id", "in", products.ids),
                ("location_id", "child_of", self.location_dest_id.ids),
                ("location_id.u_blocked", "=", False),
                ("location_id.barcode", "!=", False),
            ]
        )
        suggested_locations = orderpoints.mapped("location_id")

        return suggested_locations

    def _location_not_in_other_move_lines(self, candidate_locations):
        MoveLines = self.env["stock.move.line"]

        for _indexes, valid_locations in candidate_locations.batched(size=1000):
            # remove locations which are already used in move lines
            valid_locations -= MoveLines.search(
                [
                    ("state", "in", ["assigned", "partially_available"]),
                    ("location_dest_id", "in", valid_locations.ids),
                ]
            ).mapped("location_dest_id")

            if valid_locations:
                return valid_locations

        return self.env["stock.location"]

    def action_refactor(self):
        """Refactor all the moves in the pickings in self. May result in the
        pickings in self being deleted."""
        return self.mapped("move_lines").action_refactor()

    def _put_in_pack(self):
        mls = self.mapped("move_line_ids").filtered(
            lambda o: o.qty_done > 0 and not o.result_package_id
        )
        if mls:
            self = self.with_context(move_line_ids=mls.ids)

        return super(StockPicking, self)._put_in_pack()

    @api.constrains("batch_id")
    def _trigger_batch_confirm_and_remove(self):
        """Batch may require new pickings to be auto confirmed or removed"""
        for batches in self.env.context.get("orig_batches"), self.mapped("batch_id"):
            if batches:
                batches._assign_picks()
                batches._remove_unready_picks()
                batches._compute_state()

    @api.constrains("state")
    def _trigger_batch_state_recompute(self):
        """Changes to picking state cause batch state recompute, may also cause
        unready pickings to be removed from the batch"""
        for batches in self.env.context.get("orig_batches"), self.mapped("batch_id"):
            if batches:
                batches._remove_unready_picks()
                batches._compute_state()

    @staticmethod
    def get_stock_investigation_message(quants):
        """
        Dummy method to be overridden on a per customer basis.
        """
        return ""

    def raise_stock_inv(self, reason, quants, location):
        """Unreserve stock create stock investigation for reserve_stock and
        attempt to reserve new stock
        """
        Picking = self.env["stock.picking"]
        Group = self.env["procurement.group"]
        wh = self.env.user.get_user_warehouse()
        stock_inv_pick_type = wh.u_stock_investigation_picking_type or wh.int_type_id

        self._refine_picking(reason)
        group = Group.get_group(group_identifier=reason, create=True)

        # create new "investigation pick"
        picking = Picking.create_picking(
            quant_ids=quants.exists().ids,
            location_id=location.id,
            picking_type_id=stock_inv_pick_type.id,
            group_id=group.id,
        )

        picking_details = self.get_stock_investigation_message(quants.exists())

        if picking_details:
            picking_header = "Stock Investigation created with: <br>"
            picking.message_post(body=_(picking_header + picking_details))

        # Try to re-assign the picking after, by creating the
        # investigation, we've reserved the problematic stock
        self.action_assign()

    def search_for_pickings(self, picking_type_id, picking_priorities, limit=1, domain=None):
        """Search for next available picking based on
        picking type and priorities
        """
        Users = self.env["res.users"]
        PickingType = self.env["stock.picking.type"]

        search_domain = [] if domain is None else domain

        # Extra search parameters
        search_domain.extend(
            [
                ("picking_type_id", "=", picking_type_id),
                ("state", "=", "assigned"),
                ("batch_id", "=", False),
            ]
        )

        # -1 means unbounded
        if limit == -1:
            limit = None

        if picking_priorities:
            search_domain.append(("priority", "in", picking_priorities))

        # filter pickings by location categories if they are enabled for the
        # given picking type
        picking_type = PickingType.browse(picking_type_id)
        if picking_type.u_use_location_categories:
            categories = Users.get_user_location_categories()
            if categories:
                search_domain.append(("u_location_category_id", "child_of", categories.ids))

        if picking_type.u_batch_dest_loc_not_allowed:
            search_domain.extend([("location_dest_id.u_blocked", "!=", True)])

        # Note: order should be determined by stock.picking._order
        picking = self.search(search_domain, limit=limit)

        if not picking:
            return None

        return picking

    def _reserve_stock_assign(self):
        """Perform assign of the pickings at the move level because refactoring
        may change the pickings.
        This is a function so it can be extended and/or overriden.
        """
        # Assign at the move level disabling refactoring
        moves = self.mapped("move_lines")
        moves.with_context(lock_batch_state=True, disable_move_refactor=True)._action_assign()

        # Unreserve any partially reserved lines if not allowed by the picking type
        moves._unreserve_partial_lines()

        # Refactor after unreserving
        refactored_moves = moves._action_refactor(stage="assign")

        return refactored_moves.mapped("picking_id")

    def reserve_stock(self):
        """
        Reserve stock according to the number of reservable pickings.

        If this method is called on an empty recordset it will attempt to
        reserve stock for all eligible picking types.  If the recordset is not
        empty, it will reserve stock for the picks in the recordset. If the
        recordset is not empty, it is the callers responsibility to make sure
        that the pickings belong to at most one batch, otherwise this method
        cannot respect the priority order of pickings, in this case the
        behaviour of this method is undefined.

        In either scenario the picking type flags for reserving complete
        batches and handling partial batches are respected.

        The number of reservable pickings is defined on the picking type.
        0 reservable pickings means this function should not reserve stock
        -1 reservable picking means all reservable stock should be reserved.
        """
        Picking = self.env["stock.picking"]
        PickingType = self.env["stock.picking.type"]

        if self:
            picking_types = self.mapped("picking_type_id")
        else:
            picking_types = PickingType.search(
                [("active", "=", True), ("u_num_reservable_pickings", "!=", 0)]
            )

        # We will either reserve up to the reservation limit or until all
        # available picks have been reserved, depending on the value of
        # u_num_reservable_pickings.
        # However we must also take into account the atomic batch reservation
        # flag (u_reserve_batches) and the handle partial flag
        # (u_handle_partials).

        for picking_type in picking_types:
            _logger.info("Reserving stock for picking type %r.", picking_type)

            # We want to reserve batches atomically, that is we will
            # reserve pickings until all pickings in a batch have been
            # assigned, even if we exceed the number of reservable pickings.
            # However, the value of the handle partial flag is false we
            # should not reserve stock if the batch cannot be completely
            # reserved.
            to_reserve = picking_type.u_num_reservable_pickings
            reserve_all = to_reserve == -1
            base_domain = [("picking_type_id", "=", picking_type.id), ("state", "=", "confirmed")]
            limit = 1
            processed = Picking.browse()
            by_type = lambda x: x.picking_type_id == picking_type

            while reserve_all or to_reserve > 0:

                if self:
                    # Removed processed pickings from self
                    pickings = self.filtered(by_type) - processed
                else:
                    domain = base_domain[:]
                    if processed:
                        domain.append(("id", "not in", processed.ids))
                    pickings = Picking.search(domain, limit=limit)

                if not pickings:
                    # No pickings left to process.
                    # If u_num_reservable_pickings is -1, or there are
                    # fewer available pickings that the limit, the loop must
                    # terminate here.
                    break

                batch = pickings.mapped("batch_id")
                if batch and batch.state == "draft":
                    # Add to seen pickings so that we don't try to process
                    # this batch again.
                    processed |= batch.picking_ids
                    continue

                if batch and picking_type.u_reserve_batches:
                    pickings = batch.picking_ids

                # MPS: mimic Odoo's retry behaviour
                tries = 0
                while True:

                    try:
                        with self.env.cr.savepoint():
                            pickings = pickings._reserve_stock_assign()
                            batch._compute_state()

                            processed |= pickings

                            unsatisfied = pickings.filtered(
                                lambda x: x.state not in ["assigned", "cancel", "done"]
                            )
                            mls = pickings.mapped("move_line_ids")
                            if unsatisfied:
                                # Unreserve if the picking type cannot handle partials or it
                                # can but there is nothing allocated (no stock.move.lines)
                                if not picking_type.u_handle_partials or not mls:
                                    # construct error message, report only products
                                    # that are unreservable.
                                    not_done = lambda x: x.state not in (
                                        "done",
                                        "assigned",
                                        "cancel",
                                    )
                                    moves = unsatisfied.mapped("move_lines").filtered(not_done)
                                    products = moves.mapped("product_id").name_get()
                                    picks = moves.mapped("picking_id.name")
                                    msg = (
                                        f"Unable to reserve stock for products {[p[1] for p in products]} "
                                        f"for pickings {picks}."
                                    )
                                    raise UserError(msg)
                            break
                    except UserError as e:
                        self.invalidate_cache()
                        # Only propagate the error if the function has been
                        # manually triggered
                        if self:
                            raise e
                        tries = -1
                        break
                    except OperationalError as e:
                        self.invalidate_cache()
                        if e.pgcode not in PG_CONCURRENCY_ERRORS_TO_RETRY:
                            raise
                        if tries >= MAX_TRIES_ON_CONCURRENCY_FAILURE:
                            _logger.info(
                                "%s, maximum number of tries reached" % errorcodes.lookup(e.pgcode)
                            )
                            break
                        tries += 1
                        wait_time = 1
                        _logger.info(
                            "%s, retry %d/%d in %.04f sec..."
                            % (
                                errorcodes.lookup(e.pgcode),
                                tries,
                                MAX_TRIES_ON_CONCURRENCY_FAILURE,
                                wait_time,
                            )
                        )
                        time.sleep(wait_time)
                if tries == -1:
                    continue
                if tries >= MAX_TRIES_ON_CONCURRENCY_FAILURE:
                    break

                # Incrementally commit to release picks as soon as possible and
                # allow serialisation error to propagate to respect priority
                # order
                self.env.cr.commit()
                # Only count as reserved the number of pickings at mls
                to_reserve -= len(mls.mapped("picking_id"))

                if self:
                    # Only process the specified pickings
                    break
            _logger.info("Reserving stock for picking type %r completed.", picking_type)
        return

    def action_warn_picking_precondition(self):
        """Returns a warning string for pickings if preconditions are not met."""
        self.ensure_one()
        action = self.picking_type_id.u_warn_picking_precondition
        if action:
            return self._action_warn_picking(action)
        else:
            return False

    def _action_warn_picking(self, action):
        """Return a warning string when conditions of the action are met."""
        func = getattr(self, "warn_picking_" + action)
        warning_message = func()
        return warning_message

    def warn_picking_pickings_pending(self):
        """Return a warning string when a previous picking is pending."""
        if self.u_pending:
            return "Previous pickings are not all complete."
        else:
            return False

    def mark_empty_pickings(self):
        """Mark empty pickings to be cleaned up"""
        empty_picks = self.filtered(lambda p: len(p.move_lines) == 0)
        if empty_picks:
            _logger.info(_("Flagging empty pickings for clean up: %r") % empty_picks.ids)
            empty_picks.write({"u_mark": False})

    def _prepare_new_picking_info(self, **kwargs):
        """Copy the picking information from picking onto self"""
        update_args = kwargs
        if not kwargs.get("move_lines"):
            update_args["move_lines"] = []
        if not kwargs.get("move_line_ids"):
            update_args["move_line_ids"] = []
        return update_args

    def _create_new_picking(self, **kwargs):
        """Copy the picking information from picking onto self"""
        return self.with_context(created_due_to_backorder=True).copy(
            self._prepare_new_picking_info(**kwargs)
        )

    def _get_classification_messages_for_product_picking(self):
        """Method to be override on specific functionalities"""
        return {}

    def _get_pick_details(self):
        """Getting main picking details, method can be extendable on other modules """
        self.ensure_one()
        pick_details = {
            "details": utils.md_format_label_value("Supplier Name", self.partner_id.name)
            + utils.md_format_label_value("Source Document", self.origin),
            "submitted_value": self.origin,
        }
        return pick_details

    @api.model
    def _set_original_picking_on_pickings(self):
        """
        Set the Original Picking for pickings in the system that do not currently have it set.

        Carries out update as superuser to avoid any permissions issues, as the method is simply
        setting Original Picking to the correct value, and doesn't take any input variables.

        Pickings that are not a backorder will simply have Original Picking set as theirself.

        Backorder pickings however will have Original Picking set to the 'root' picking
        of the chain.

        For example:
            - IN001 receives 3 out of 5 expected quantity, which are put into IN002
            - IN001 receives 1 out of the 2 remaining quantity, which is put into IN003
            - IN001, IN002 and IN003 will all have the same Original Picking; IN001
        """
        Picking = self.env["stock.picking"].sudo().with_context(prefetch_fields=False)

        picking_ids = Picking.search([("u_original_picking_id", "=", False)], order="id").ids
        picking_count = len(picking_ids)

        if picking_count:
            _logger.info("Setting Original Picking value on %s pickings", picking_count)

            update_query = """
                WITH
                RECURSIVE recursive_picking
                AS (
                    SELECT
                    p.id,
                    p.id AS orig_picking_id,
                    1 DEPTH
                    FROM stock_picking p

                UNION ALL
                    SELECT
                    rp.id,
                    p.backorder_id,
                    rp.depth+ 1
                    FROM
                    recursive_picking rp
                    JOIN
                    stock_picking p ON rp.orig_picking_id = p.id
                    WHERE
                    p.backorder_id IS NOT NULL
                )

                UPDATE
                stock_picking p
                SET
                write_uid = %(user_id)s,
                write_date = now() at time zone 'UTC',
                u_original_picking_id = picking_info.orig_picking_id

                FROM
                (
                SELECT
                DISTINCT ON (rp.id)
                rp.id as rp_id,
                rp.orig_picking_id as orig_picking_id
                FROM
                recursive_picking rp
                ORDER BY id, depth DESC
                ) picking_info

                WHERE
                p.id IN %(picking_ids)s
                AND
                picking_info.rp_id = p.id
            """
            update_values = {
                "user_id": SUPERUSER_ID,
                "picking_ids": tuple(picking_ids),
            }
            self.env.cr.execute(update_query, update_values)

            _logger.info("Original Picking value set on %s pickings", picking_count)

    def _create_backorder(self, backorder_moves=[]):
        """Add `created_due_to_backorder` to context when creating a backorder through UI"""
        self = self.with_context(created_due_to_backorder=True)
        return super()._create_backorder(backorder_moves)

    def action_detailed_view(self):
        """Function will call the picking form with a context view_all_fields set to True
        which will un hide the fields that were hidden"""
        form_view = self.env.ref("stock.view_picking_form")
        return self.base_model_detailed_view(self._name, form_view)

    def open_related_pickings(self, picking_ids, related_pickings_description):
        """Main method which is called from all models to redirect to a form view with context
        view_all_fields True in order to remove the fields that are configured in helpers"""
        self.ensure_one()
        if len(picking_ids) == 1:
            form_view = self.env.ref("stock.view_picking_form")
            action = {
                "type": "ir.actions.act_window",
                "view_type": "form",
                "view_model": "form",
                "views": [(form_view.id, "form")],
                "res_model": "stock.picking",
                "view_id": form_view.id,
                "res_id": picking_ids[0],
                "context": {"view_all_fields": False},
            }
        else:
            action = {
                "type": "ir.actions.act_window",
                "name": _("%s") % related_pickings_description,
                "res_model": "stock.picking",
                "view_type": "form",
                "view_mode": "tree,form",
                "domain": [("id", "in", picking_ids)],
                "context": {"view_all_fields": False},
            }
        return action

    def open_first_pickings(self):
        """Open first pickings button will redirect to first pickings"""
        return self.open_related_pickings(
            self.u_first_picking_ids.ids, "First Pickings"
        )

    def open_prev_pickings(self):
        """Open prev pickings button will redirect to previous pickings"""
        return self.open_related_pickings(
            self.u_prev_picking_ids.ids, "Previous Pickings"
        )

    def open_next_pickings(self):
        """Open next pickings button will redirect to next pickings"""
        return self.open_related_pickings(
            self.u_next_picking_ids.ids, "Next Pickings"
        )

    def open_back_orders(self):
        """Open back orders button will redirect to created back orders"""
        return self.open_related_pickings(
            self.u_created_back_orders.ids, "Created Back Orders"
        )
