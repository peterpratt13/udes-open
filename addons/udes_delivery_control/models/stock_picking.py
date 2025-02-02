from odoo import fields, models, api
from odoo.addons.udes_stock import utils

EXTRA_FIELDS = {
    "u_user_id",
    "u_location_id",
    "u_vehicle_arrival_date",
    "u_week",
    "u_unloading_start_date",
    "u_unloading_end_date",
    "u_vehicle_type",
    "u_unloading_time_taken",
    "u_trailer_number",
    "u_lane_number",
    "u_pallet_count",
    "u_stillage_count",
    "u_box_count",
    "u_is_planned",
    "u_is_late",
    "u_is_fast_track",
    # backloading fields
    "u_loading_type",
    "u_backload_ids",
}


class StockPicking(models.Model):

    _inherit = "stock.picking"

    u_is_delivery_control = fields.Boolean(
        related="picking_type_id.u_is_delivery_control",
        readonly=1,
        states={"done": [("readonly", True)], "cancel": [("readonly", True)]},
    )
    u_extras_id = fields.Many2one("stock.picking.extras", copy=False, ondelete="cascade")
    u_loading_type = fields.Selection(
        related="u_extras_id.loading_type",
        states={"done": [("readonly", True)], "cancel": [("readonly", True)]},
    )

    # Common fields
    u_user_id = fields.Many2one(
        "res.users",
        related="u_extras_id.user_id",
        states={"done": [("readonly", True)], "cancel": [("readonly", True)]},
    )
    u_vehicle_arrival_date = fields.Datetime(
        related="u_extras_id.vehicle_arrival_date",
        states={"done": [("readonly", True)], "cancel": [("readonly", True)]},
    )
    u_week = fields.Integer(
        related="u_extras_id.week",
        states={"done": [("readonly", True)], "cancel": [("readonly", True)]},
    )
    u_vehicle_type = fields.Many2one(
        "stock.picking.vehicle.type",
        related="u_extras_id.vehicle_type",
        states={"done": [("readonly", True)], "cancel": [("readonly", True)]},
    )
    u_trailer_number = fields.Integer(
        related="u_extras_id.trailer_number",
        states={"done": [("readonly", True)], "cancel": [("readonly", True)]},
    )
    u_is_planned = fields.Boolean(
        related="u_extras_id.is_planned",
        states={"done": [("readonly", True)], "cancel": [("readonly", True)]},
    )
    u_is_late = fields.Boolean(
        related="u_extras_id.is_late",
        states={"done": [("readonly", True)], "cancel": [("readonly", True)]},
    )
    u_is_fast_track = fields.Boolean(
        related="u_extras_id.is_fast_track",
        states={"done": [("readonly", True)], "cancel": [("readonly", True)]},
    )

    # Unloading Fields
    u_is_unload = fields.Boolean(related="u_extras_id.is_unload")
    u_location_id = fields.Many2one(
        "stock.location",
        related="u_extras_id.location_id",
        states={"done": [("readonly", True)], "cancel": [("readonly", True)]},
    )
    u_unloading_start_date = fields.Datetime(
        related="u_extras_id.unloading_start_date",
        states={"done": [("readonly", True)], "cancel": [("readonly", True)]},
    )
    u_unloading_end_date = fields.Datetime(
        related="u_extras_id.unloading_end_date",
        states={"done": [("readonly", True)], "cancel": [("readonly", True)]},
    )
    u_unloading_time_taken = fields.Float(
        readonly=True,
        related="u_extras_id.unloading_time_taken",
        states={"done": [("readonly", True)], "cancel": [("readonly", True)]},
    )
    u_lane_number = fields.Char(
        related="u_extras_id.lane_number",
        states={"done": [("readonly", True)], "cancel": [("readonly", True)]},
    )
    u_pallet_count = fields.Integer(
        related="u_extras_id.pallet_count",
        states={"done": [("readonly", True)], "cancel": [("readonly", True)]},
    )
    u_stillage_count = fields.Integer(
        related="u_extras_id.stillage_count",
        states={"done": [("readonly", True)], "cancel": [("readonly", True)]},
    )
    u_box_count = fields.Integer(
        related="u_extras_id.box_count",
        states={"done": [("readonly", True)], "cancel": [("readonly", True)]},
    )

    # Backloading Fields
    u_is_backload = fields.Boolean(related="u_extras_id.is_backload")
    u_backload_ids = fields.One2many(related="u_extras_id.backload_ids")
    u_backload_added = fields.Boolean(
        compute="_compute_backload_added",
        string="Backload Added",
        help="True if a backload record exists on the picking",
    )

    u_delivery_control_picking_id = fields.Many2one(
        "stock.picking",
        string="Originating Delivery Control Picking",
        readonly=True,
        index=True,
        copy=False,
    )
    u_goods_in_picking_id = fields.Many2one(
        "stock.picking", string="Generated Goods In Picking", readonly=True, index=True, copy=False
    )

    _sql_constraints = [
        (
            "extras_id_uniq",
            "UNIQUE (u_extras_id)",
            "You can not have two pickings associated with the same picking extras!",
        )
    ]

    @api.depends(
        "move_lines",
        "move_lines.move_orig_ids",
        "move_lines.move_dest_ids",
        "move_lines.move_orig_ids.picking_id",
        "move_lines.move_dest_ids.picking_id",
        "u_goods_in_picking_id",
        "u_delivery_control_picking_id",
    )
    def _compute_related_picking_ids(self):
        """Override to link Delivery Control and Goods In pickings"""
        super(StockPicking, self)._compute_related_picking_ids()

        goods_in_type = self.env.ref("stock.picking_type_in")

        for picking in self.filtered(
            lambda p: p.u_is_delivery_control or p.picking_type_id == goods_in_type
        ):
            if picking.u_is_delivery_control and picking.u_goods_in_picking_id:
                picking.u_next_picking_ids |= picking.u_goods_in_picking_id
            elif picking.u_delivery_control_picking_id:
                picking.u_prev_picking_ids |= picking.u_delivery_control_picking_id

    @api.depends(
        "move_lines",
        "move_lines.move_orig_ids",
        "move_lines.move_orig_ids.picking_id",
        "u_goods_in_picking_id",
        "u_delivery_control_picking_id",
    )
    def _compute_first_picking_ids(self):
        """
        Override to replace first pickings that are Goods In pickings with their linked
        Delivery Control picking, if applicable

        Also set first pickings for Delivery Control pickings to themselves
        """
        super(StockPicking, self)._compute_first_picking_ids()

        goods_in_type = self.env.ref("stock.picking_type_in")

        for picking in self:
            first_pickings = picking.u_first_picking_ids

            if picking.u_is_delivery_control:
                first_pickings = picking
            elif picking.picking_type_id == goods_in_type and picking.u_delivery_control_picking_id:
                first_pickings -= picking
                first_pickings |= picking.u_delivery_control_picking_id
            else:
                first_pickings_types = first_pickings.mapped("picking_type_id")
                delivery_control_pickings = first_pickings.mapped("u_delivery_control_picking_id")

                if goods_in_type in first_pickings_types and delivery_control_pickings:
                    goods_in_pickings_to_remove = first_pickings.filtered(
                        "u_delivery_control_picking_id"
                    )
                    # Remove Goods In Pickings and replace them with their linked
                    # Delivery Control pickings
                    first_pickings -= goods_in_pickings_to_remove
                    first_pickings |= delivery_control_pickings

            picking.u_first_picking_ids = first_pickings

    @api.depends("state", "move_lines", "picking_type_id")
    def _compute_show_mark_as_todo(self):
        """Override to show Mark as Todo button for draft Delivery Control pickings"""
        super()._compute_show_mark_as_todo()

        for picking in self.filtered("u_is_delivery_control"):
            picking.show_mark_as_todo = picking.state == "draft"

    @api.depends("u_backload_ids")
    def _compute_backload_added(self):
        """Set to True if Backload record has been added to Picking"""
        for picking in self.filtered(lambda p: p.u_is_backload and p.u_backload_ids):
            picking.u_backload_added = True

    @api.onchange("picking_type_id")
    def _onchange_picking_type_id(self):
        for record in self:
            if record.picking_type_id.u_is_delivery_control:
                record.u_loading_type = "unload"
                record.u_is_unload = True

    @api.onchange("u_loading_type")
    def _onchange_loading_type(self):
        """Compute u_is_backload and u_is_unload from u_loading_type"""
        for record in self:
            if record.u_loading_type:
                record.u_is_unload = "unload" in record.u_loading_type
                record.u_is_backload = "backload" in record.u_loading_type

    def _create_picking_extras_data(self, values):
        """ Create a transport information for each picking that doesn't
            have it.
        """
        PickingExtras = self.env["stock.picking.extras"]
        filtered_extras = {k[2:]: values[k] for k in EXTRA_FIELDS if k in values and values[k]}
        if filtered_extras:
            for record in self:
                if not record.u_extras_id:
                    filtered_extras["picking_ids"] = [(4, record.id)]
                    PickingExtras.create(filtered_extras)
                else:
                    record.u_extras_id.write(filtered_extras)
            values = {k: v for k, v in values.items() if k not in EXTRA_FIELDS}

        return values

    @api.multi
    def write(self, values):
        values = self._create_picking_extras_data(values)
        res = super(StockPicking, self).write(values)
        return res

    @api.model
    def create(self, values):
        picking_type_dc = self.env.ref("udes_delivery_control.picking_type_delivery_control")
        picking_type_id = values.get("picking_type_id")
        if (
            picking_type_id
            and picking_type_id == picking_type_dc.id
            and not values.get("u_loading_type")
        ):
            # Set loading type to unload if picking is of delivery control type
            # Note: u_loading_type is of related type field and compute attribute
            # is not compatible/called. Hence, we are setting it's default value in create method
            values["u_loading_type"] = "unload"

        res = super(StockPicking, self).create(values)
        res._create_picking_extras_data(values)
        return res

    @api.model
    def _default_u_supplier_id(self):
        return self.partner_id

    def button_validate(self):
        """Override to allow Delivery Control pickings to validate without stock moves"""
        self.ensure_one()

        if self.u_is_delivery_control:
            return self.action_delivery_control_done()
        else:
            return super(StockPicking, self).button_validate()

    def action_confirm(self):
        """Override to confirm Delivery Control pickings without interacting with stock moves"""
        delivery_control_pickings = self.filtered("u_is_delivery_control")
        other_pickings = self - delivery_control_pickings

        for picking in delivery_control_pickings:
            picking.write({"state": "assigned"})

        res = True

        if other_pickings:
            res = super(StockPicking, other_pickings).action_confirm()

        return res

    def action_cancel(self):
        """
        Override to mark Delivery Control pickings as cancelled, and carry out action_cancel
        on linked Goods In pickings
        """
        delivery_control_pickings = self.filtered("u_is_delivery_control")
        other_pickings = self - delivery_control_pickings

        for picking in delivery_control_pickings:
            picking.write({"state": "cancel"})

            if picking.u_goods_in_picking_id:
                other_pickings |= picking.u_goods_in_picking_id

        res = True

        if other_pickings:
            res = super(StockPicking, other_pickings).action_cancel()
        return res

    def action_done(self):
        """Override to call action_delivery_control_done for Delivery Control pickings"""
        delivery_control_pickings = self.filtered("u_is_delivery_control")
        other_pickings = self - delivery_control_pickings

        res = delivery_control_pickings.action_delivery_control_done()

        if other_pickings:
            res = super(StockPicking, other_pickings).action_done()

        return res

    def action_delivery_control_done(self):
        """
        Mark Delivery Control picking as done.

        Generate a Goods In picking if Delivery Control picking not already linked with one.

        Otherwise check if linked Goods in Picking is in 'Waiting Another Operation' state and
        confirm it if so.
        """
        for picking in self.filtered("u_is_delivery_control"):
            picking.write({"state": "done", "date_done": fields.Datetime.now()})

            if not picking.u_goods_in_picking_id and picking.u_is_unload:
                self.create_goods_in_from_delivery_control()
            elif picking.u_goods_in_picking_id.state == "waiting":
                picking.u_goods_in_picking_id.action_confirm()

        return True

    def create_goods_in_from_delivery_control(self, **kwargs):
        """Create Goods In picking from Delivery Control picking and add link between both"""
        self.ensure_one()

        goods_in_type = self.env.ref("stock.picking_type_in")
        goods_in_picking = self.browse()

        if self.u_is_delivery_control:
            vals = {
                "picking_type_id": goods_in_type.id,
                "location_id": goods_in_type.default_location_src_id.id,
                "location_dest_id": goods_in_type.default_location_dest_id.id,
                "u_delivery_control_picking_id": self.id,
                "origin": self.origin,
                "partner_id": self.partner_id.id if self.partner_id else False,
            }
            vals.update(kwargs)
            goods_in_picking = self.create(vals)

            self.write({"u_goods_in_picking_id": goods_in_picking.id})

        return goods_in_picking

    def _get_pick_details(self):
        pick_details = super()._get_pick_details()
        if self.u_delivery_control_picking_id and self.u_delivery_control_picking_id.u_lane_number:
            pick_details["details"] += utils.md_format_label_value(
                    "Lane", self.u_delivery_control_picking_id.u_lane_number
                )
        return pick_details
