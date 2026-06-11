TELEGRAM_MAX_MESSAGE_SIZE = 4096
ENV_TARGET_CHAT_ID = 'TELEGRAM_TARGET_CHAT_ID'
ENV_BOT_TOKEN = 'TELEGRAM_BOT_TOKEN'
ENV_MAINTAINER_IDS = 'TELEGRAM_MAINTAINER_IDS'
ENV_DATABASE_NAME = 'DATABASE_NAME'
# Токен football-data.org для авто-завершения матчей. Пустой/отсутствует — фича выключена.
ENV_FOOTBALL_DATA_TOKEN = 'FOOTBALL_DATA_API_TOKEN'

# Один активный турнир за раз — singleton-документ в коллекции 'tournament'.
ACTIVE_TOURNAMENT_ID = 'active'
