# Маппинг русских названий команд ЧМ-2026 (как мейнтейнер вводит их в /add_event)
# на данные football-data.org. Сопоставление идёт не по одному точному имени,
# а по набору ключей: варианты английского названия (name/shortName могут отличаться:
# "Czech Republic" vs "Czechia") + трёхбуквенный FIFA-код (tla) как самый надёжный якорь.

# (русские варианты написания, ...), (английские варианты + FIFA-код, ...)
# Первый английский вариант — каноническое имя для логов.
_TEAMS: list[tuple[tuple[str, ...], tuple[str, ...]]] = [
    (('Алжир',), ('Algeria', 'ALG')),
    (('Аргентина',), ('Argentina', 'ARG')),
    (('Австралия',), ('Australia', 'AUS')),
    (('Австрия',), ('Austria', 'AUT')),
    (('Бельгия',), ('Belgium', 'BEL')),
    (('Босния и Герцеговина', 'Босния'), ('Bosnia and Herzegovina', 'Bosnia-Herzegovina', 'Bosnia', 'BIH')),
    (('Бразилия',), ('Brazil', 'BRA')),
    (('Канада',), ('Canada', 'CAN')),
    (('Кабо-Верде', 'Кабо Верде'), ('Cape Verde', 'Cabo Verde', 'Cape Verde Islands', 'CPV')),
    (('Колумбия',), ('Colombia', 'COL')),
    (('Хорватия',), ('Croatia', 'CRO')),
    (('Кюрасао',), ('Curaçao', 'Curacao', 'CUW')),
    (('Чехия',), ('Czech Republic', 'Czechia', 'CZE')),
    (('ДР Конго', 'Конго', 'Демократическая Республика Конго'), ('DR Congo', 'Congo DR', 'Congo', 'COD')),
    (('Эквадор',), ('Ecuador', 'ECU')),
    (('Египет',), ('Egypt', 'EGY')),
    (('Англия',), ('England', 'ENG')),
    (('Франция',), ('France', 'FRA')),
    (('Германия',), ('Germany', 'GER', 'DEU')),
    (('Гана',), ('Ghana', 'GHA')),
    (('Гаити',), ('Haiti', 'HAI', 'HTI')),
    (('Иран',), ('Iran', 'IR Iran', 'IRN')),
    (('Ирак',), ('Iraq', 'IRQ')),
    (("Кот-д'Ивуар", 'Кот-дИвуар', 'Берег Слоновой Кости'), ("Ivory Coast", "Côte d'Ivoire", "Cote d'Ivoire", 'CIV')),
    (('Япония',), ('Japan', 'JPN')),
    (('Иордания',), ('Jordan', 'JOR')),
    (('Мексика',), ('Mexico', 'MEX')),
    (('Марокко',), ('Morocco', 'MAR')),
    (('Нидерланды', 'Голландия'), ('Netherlands', 'NED', 'NLD')),
    (('Новая Зеландия',), ('New Zealand', 'NZL')),
    (('Норвегия',), ('Norway', 'NOR')),
    (('Панама',), ('Panama', 'PAN')),
    (('Парагвай',), ('Paraguay', 'PAR', 'PRY')),
    (('Португалия',), ('Portugal', 'POR', 'PRT')),
    (('Катар',), ('Qatar', 'QAT')),
    (('Саудовская Аравия',), ('Saudi Arabia', 'KSA', 'SAU')),
    (('Шотландия',), ('Scotland', 'SCO')),
    (('Сенегал',), ('Senegal', 'SEN')),
    (('ЮАР', 'Южная Африка'), ('South Africa', 'RSA', 'ZAF')),
    (('Южная Корея', 'Корея', 'Республика Корея'), ('South Korea', 'Korea Republic', 'KOR')),
    (('Испания',), ('Spain', 'ESP')),
    (('Швеция',), ('Sweden', 'SWE')),
    (('Швейцария',), ('Switzerland', 'SUI', 'CHE')),
    (('Тунис',), ('Tunisia', 'TUN')),
    (('Турция',), ('Turkey', 'Türkiye', 'Turkiye', 'TUR')),
    (('США',), ('USA', 'United States', 'United States of America', 'US')),
    (('Уругвай',), ('Uruguay', 'URU', 'URY')),
    (('Узбекистан',), ('Uzbekistan', 'UZB')),
]


def normalize(name: str) -> str:
    # Типографский апостроф (’) приводим к обычному ('), чтобы "Кот-д’Ивуар" == "Кот-д'Ивуар".
    return name.strip().replace('’', "'").casefold()


_RU_TO_EN_KEYS: dict[str, frozenset] = {}
_RU_TO_CANONICAL_EN: dict[str, str] = {}
for _ru_names, _en_keys in _TEAMS:
    _key_set = frozenset(normalize(key) for key in _en_keys)
    for _ru_name in _ru_names:
        _RU_TO_EN_KEYS[normalize(_ru_name)] = _key_set
        _RU_TO_CANONICAL_EN[normalize(_ru_name)] = _en_keys[0]


def get_api_keys(ru_name: str) -> frozenset | None:
    # Нормализованные английские ключи для сопоставления с {name, shortName, tla} из API.
    return _RU_TO_EN_KEYS.get(normalize(ru_name))


def get_english_name(ru_name: str) -> str | None:
    return _RU_TO_CANONICAL_EN.get(normalize(ru_name))
