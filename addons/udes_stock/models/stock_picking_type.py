from odoo import fields, models


TARGET_STORAGE_FORMAT_OPTIONS = [
    ("pallet_products", "Pallet of products"),
    ("pallet_packages", "Pallet of packages"),
    ("package", "Packages"),
    ("product", "Products"),
]


class StockPickingType(models.Model):
    _inherit = "stock.picking.type"

    u_user_scans = fields.Selection(
        [("pallet", "Pallets"), ("package", "Packages"), ("product", "Products")],
        string="What the User Scans",
        help="What the user scans when asked to scan something from pickings of this type",
    )
    u_target_storage_format = fields.Selection(
        TARGET_STORAGE_FORMAT_OPTIONS, string="Target Storage Format"
    )
    u_under_receive = fields.Boolean(
        string="Under Receive",
        default=False,
        help="If True, allow less items than the expected quantity in a move line.",
    )
    u_over_receive = fields.Boolean(
        string="Over Receive",
        default=True,
        help="If True, allow more items than the expected quantity in a move line.",
    )
    u_scan_parent_package_end = fields.Boolean(
        string="Scan Parent Package at the End",
        default=False,
        help="If True, the user is asked to scan parent package on drop off.",
    )
    u_auto_unlink_empty = fields.Boolean(
        string="Auto Unlink Empty",
        default=True,
        help="""
        Flag to indicate whether to unlink empty pickings when searching for any empty picking in
        the system.
        """,
    )
    u_enable_unpickable_items = fields.Boolean(
        string="Enable Unpickable Items",
        default=False,
        help="Flag to indicate if the current picking type should support handling of unpickable items.",
    )

    def get_action_picking_tree_draft(self):
        return self._get_action("udes_stock.action_picking_tree_draft")
