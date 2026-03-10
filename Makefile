setup:
	python3 -m venv .venv
	. .venv/bin/activate; pip install -r requirements.txt

bot:
	. .venv/bin/activate; python -m local_tg_bot.run
