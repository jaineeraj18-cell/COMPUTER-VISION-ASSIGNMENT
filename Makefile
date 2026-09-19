.PHONY: install test discover replay replay-error operator variant

install:
	pip install -r requirements.txt
	python -m playwright install chromium

test:
	pytest

operator:
	PYTHONPATH=src python -m cua.escalation.operator_console

variant:
	python target_app/variant_app.py

discover:
	PYTHONPATH=src python -m cua.cli discover \
	  --url https://www.saucedemo.com/ \
	  --id saucedemo.checkout_review --app saucedemo \
	  --goal "Sign in, add the product named item_name to the cart, open the cart, start checkout, fill the checkout information form with first_name, last_name and postal_code, and stop on the checkout overview screen. Do not place the order." \
	  --secret username=SAUCE_USERNAME --secret password=SAUCE_PASSWORD \
	  --param item_name="Sauce Labs Backpack" \
	  --param first_name=Alex --param last_name=Rivera --param postal_code=37402

replay:
	PYTHONPATH=src python -m cua.cli replay --capability saucedemo.checkout_review \
	  --secret username=SAUCE_USERNAME --secret password=SAUCE_PASSWORD \
	  --param item_name="Sauce Labs Bike Light" \
	  --param first_name=Alex --param last_name=Rivera --param postal_code=37402

replay-error:
	PYTHONPATH=src python -m cua.cli replay --capability saucedemo.checkout_review \
	  --secret username=SAUCE_LOCKED_USER --secret password=SAUCE_PASSWORD \
	  --param item_name="Sauce Labs Backpack" \
	  --param first_name=Alex --param last_name=Rivera --param postal_code=37402
