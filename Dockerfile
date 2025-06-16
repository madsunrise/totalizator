FROM python:3.12.4-slim

# Install locales package:
RUN apt-get update && apt-get install -y locales && rm -rf /var/lib/apt/lists/*

# Generate the ru_RU.UTF-8 locale:
RUN sed -i '/ru_RU.UTF-8/s/^# //g' /etc/locale.gen && locale-gen

# Set the locale environment variable
ENV LANG ru_RU.UTF-8
ENV LANGUAGE ru_RU:ru
ENV LC_ALL ru_RU.UTF-8

WORKDIR /app
COPY requirements.txt .
RUN pip3 install -r requirements.txt
# благодаря двум COPY если мы меняем только код, то предыдущие слои кэшируются и зависимости заново не ставятся.
COPY . .
ENTRYPOINT python3 ./main.py

# сборка: docker buildx build --platform linux/amd64 -t totalizator:v1 .
# сохранение: docker save -o totalizator_v1.tar totalizator:v1
# выгрузка: docker load -i totalizator_v1.tar
# запуск docker run --name totalizator --network=host -d --restart unless-stopped -e TELEGRAM_TARGET_CHAT_ID=0 -e TELEGRAM_BOT_TOKEN=your_token -e TELEGRAM_MAINTAINER_ID=0 totalizator:v1