FROM python:3.12-slim
WORKDIR /bot
RUN pip install --no-cache-dir "discord.py>=2.4" "httpx>=0.27"
COPY discord_registration.py discord_bot.py gallery_praise.py warden_bot.py ./
CMD ["python", "discord_bot.py"]
