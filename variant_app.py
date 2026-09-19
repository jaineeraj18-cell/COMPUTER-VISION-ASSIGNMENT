"""A second "tenant" running the same vendor product.

Purpose: two demos the public target cannot give us.

1. Cross-tenant reuse. This app is the same flow as the public target with a
   different skin and different wording ("Add to basket", "Sign On", "Next"),
   which is exactly what one vendor product looks like across two credit
   unions. A capability recorded against the public target replays here with a
   handful of per-variant overrides and no re-recording - see
   `scripts/add_variant_override.py`.

2. Error injection on demand. `?fault=` lets us reproduce the runtime
   conditions that matter (maintenance interstitial, not-found, permission
   denial, slow load, session expiry) without waiting for the public site to
   misbehave.

The markup is deliberately hostile in the way legacy back-office apps are:
table layout, no test ids, no <label for>, names carried only by adjacent
cells - which is what `harvest.js` anchor detection exists for.
"""

from __future__ import annotations

import os
import time

from flask import Flask, redirect, request, session

app = Flask(__name__)
app.secret_key = "demo-only-not-a-secret"

ITEMS = {
    "Sauce Labs Backpack": "29.99",
    "Sauce Labs Bike Light": "9.99",
    "Sauce Labs Bolt T-Shirt": "15.99",
}

_SHELL = """<!doctype html><html><head><title>Northbank Servicing - {title}</title>
<style>body{{font:13px verdana;margin:0}}table{{border-collapse:collapse}}
td{{padding:4px 8px;border:1px solid #ccd}} .hdr{{background:#123a6b;color:#fff;padding:8px}}
.err{{color:#a00;font-weight:bold}}</style></head><body>
<div class="hdr">NORTHBANK SERVICING CONSOLE &nbsp; v4.2.1</div>
{body}</body></html>"""


def page(title: str, body: str) -> str:
    return _SHELL.format(title=title, body=body)


def fault() -> str:
    return request.args.get("fault", "")


def guard():
    """Returns a response to short-circuit with, or None."""
    if fault() == "slow":
        time.sleep(4)
    if fault() == "maintenance":
        return page(
            "Notice",
            "<table><tr><td><p>Scheduled maintenance window begins at 02:00. "
            "Processing may be delayed.</p>"
            '<form method="get"><input type="submit" value="Continue"></form>'
            "</td></tr></table>",
        )
    if fault() == "expire":
        session.clear()
        return page("Session", '<p class="err">Your session has expired. Please sign on again.</p>')
    if fault() == "error":
        return page("Error", '<p class="err">Internal server error (ref 500-A1).</p>')
    if fault() == "denied":
        return page("Denied", '<p class="err">You do not have permission to view this record.</p>')
    return None


@app.get("/variant/")
def login_form():
    blocked = guard()
    if blocked:
        return blocked
    return page(
        "Sign On",
        """<table><tr><td>User</td><td><input name="u" type="text"></td></tr>
        <tr><td>Pass</td><td><input name="p" type="password"></td></tr></table>
        <form method="post" action="/variant/signon">
        <input type="hidden" name="carry" value="1">
        <input type="submit" value="Sign On"></form>""",
    )


@app.post("/variant/signon")
def signon():
    session["user"] = "teller"
    return redirect("/variant/inventory")


@app.get("/variant/inventory")
def inventory():
    blocked = guard()
    if blocked:
        return blocked
    rows = "".join(
        f"<tr><td>{name}</td><td>${price}</td>"
        f'<td><form method="post" action="/variant/add">'
        f'<input type="hidden" name="item" value="{name}">'
        f'<input type="submit" value="Add to basket"></form></td></tr>'
        for name, price in ITEMS.items()
    )
    return page(
        "Products",
        f"<h3>Product catalogue</h3><table>{rows}</table>"
        '<p><a href="/variant/cart">View basket</a></p>',
    )


@app.post("/variant/add")
def add():
    item = request.form.get("item", "")
    if item not in ITEMS:
        return page("Not found", '<p class="err">No such item found in the catalogue.</p>')
    session["item"] = item
    return redirect("/variant/cart")


@app.get("/variant/cart")
def cart():
    blocked = guard()
    if blocked:
        return blocked
    item = session.get("item")
    if not item:
        return page("Basket", "<p>Your basket is empty.</p>")
    return page(
        "Basket",
        f"<h3>Basket</h3><table><tr><td>{item}</td><td>${ITEMS[item]}</td></tr></table>"
        '<p><a href="/variant/checkout-step-one">Check out</a></p>',
    )


@app.get("/variant/checkout-step-one")
def step_one():
    blocked = guard()
    if blocked:
        return blocked
    return page(
        "Your details",
        """<h3>Your details</h3>
        <form method="post" action="/variant/checkout-step-one">
        <table>
        <tr><td>First Name</td><td><input name="first" type="text"></td></tr>
        <tr><td>Last Name</td><td><input name="last" type="text"></td></tr>
        <tr><td>Postal Code</td><td><input name="zip" type="text"></td></tr>
        </table><input type="submit" value="Next"></form>""",
    )


@app.post("/variant/checkout-step-one")
def step_one_post():
    if not request.form.get("zip"):
        return page(
            "Your details",
            '<p class="err">Error: Postal Code is required</p>'
            '<form method="get" action="/variant/checkout-step-one">'
            '<input type="submit" value="Back"></form>',
        )
    session["zip"] = request.form.get("zip")
    return redirect("/variant/checkout-step-two")


@app.get("/variant/checkout-step-two")
def step_two():
    blocked = guard()
    if blocked:
        return blocked
    item = session.get("item")
    if not item:
        return redirect("/variant/inventory")
    price = float(ITEMS[item])
    tax = round(price * 0.08, 2)
    return page(
        "Order Review",
        f"""<h3>Order Review</h3>
        <table><tr><td>{item}</td><td>${price:.2f}</td></tr>
        <tr><td>Item total</td><td>${price:.2f}</td></tr>
        <tr><td>Tax</td><td>${tax:.2f}</td></tr>
        <tr><td>Total</td><td>${price + tax:.2f}</td></tr></table>
        <form method="post" action="/variant/finish">
        <input type="submit" value="Finish"></form>""",
    )


@app.post("/variant/finish")
def finish():
    return page("Complete", "<h3>Thank you for your order</h3>")


def main() -> None:
    port = int(os.environ.get("CUA_VARIANT_PORT", "8799"))
    print(f"Tenant-variant app on http://127.0.0.1:{port}/variant/")
    app.run(port=port, debug=False)


if __name__ == "__main__":
    main()
