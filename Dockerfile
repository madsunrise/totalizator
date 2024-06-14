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
COPY . .
RUN pip3 install -r requirements.txt
ENTRYPOINT python3 ./main.py