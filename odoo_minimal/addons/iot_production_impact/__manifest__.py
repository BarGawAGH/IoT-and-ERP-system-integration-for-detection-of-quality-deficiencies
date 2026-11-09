{
    "name": "IoT Production Impact",
    "version": "20.0.1.0.0",
    "author": "Research prototype contributors",
    "summary": "Audited production decisions, quarantine, rework and material planning",
    "category": "Manufacturing",
    "license": "LGPL-3",
    "depends": ["iot_poc_community", "mrp", "purchase_stock"],
    "data": [
        "security/groups.xml",
        "security/ir.access.csv",
        "wizard/disposition_views.xml",
        "views/case_views.xml",
        "views/production_views.xml",
    ],
    "application": True,
    "installable": True,
    "auto_install": False,
}
