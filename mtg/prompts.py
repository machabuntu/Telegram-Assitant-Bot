"""AI prompt templates for /mcg."""

CROP_SYSTEM = """\
You are a computer vision assistant. Analyze images and return crop coordinates as JSON only."""

CROP_USER = """\
Проанализируй изображение. Найди главный объект (существо, лицо, предмет). \
Определи координаты самого оптимального вертикального прямоугольника (соотношение сторон 5:7, ширина к высоте), \
который включает этот объект полностью и выглядит композиционно приятно. \
Прямоугольник должен быть вертикальным и максимально использовать формат 5:7. \
Верни координаты строго в формате JSON: {"xmin": 0-1000, "ymin": 0-1000, "xmax": 0-1000, "ymax": 0-1000}. \
Координаты нормализованы от 0 до 1000. Никакого другого текста."""

CARD_TEXT_SYSTEM = """\
You are an expert Magic: The Gathering card designer with a sharp sense of humor. Given an image, \
design a complete MTG card inspired by it. The card should be funny and witty. \
Return ONLY structured fields — no commentary, no markdown fences."""

CARD_TEXT_USER = """\
Проанализируй изображение и придумай карту Magic: The Gathering, вдохновлённую им.

Карта должна быть юморной и отражать культурные особенности России: смешное название, забавные правила или способности, ироничный flavor-текст. \
Юмор может быть абсурдным, сатиричным или игривым, но текст должен оставаться читаемым и узнаваемым как карта MTG.

Разрешённые типы карт:
- standard — Creature, Instant, Sorcery, Enchantment, Enchantment-Aura или Artifact
- planeswalker — Planeswalker с ровно 3 способностями

Запрещено: Land, Saga, Token и любые другие типы.

Весь игровой текст карты (название, type line, rules, flavor, способности) пиши на русском языке.

КРИТИЧЕСКИ ВАЖНО — формат символов маны (сборщик карты понимает ТОЛЬКО так):
- MANA_COST: каждый символ в фигурных скобках подряд, БЕЗ пробелов. Примеры: {2}{R}{R}, {1}{W}{U}, {X}{G}.
  Используй ТОЛЬКО латинские буквы W, U, B, R, G, C, X, S, T и цифры 0–20 внутри скобок.
  НЕ пиши 2RR, {2RR}, «2 красных», русские названия цветов или слова tap/повернуть в MANA_COST.
- RULES_TEXT и тексты способностей planeswalker: символы маны и {T} (поворот) тоже ТОЛЬКО в скобках: {W}, {2}, {T}, {R/G}.
  Для стоимости способности в тексте используй тот же формат, напр. «{T}: нанесите 1 урон. {R}: ...»

Не включай reminder text для ключевых слов.
Не используй сокращения и ~ вместо имени карты — всегда пиши полное название.

Верни поля (одно на строку, через двоеточие):

CARD_TYPE: standard или planeswalker
NAME: <название карты>
COLORS: <цветовая identity: W, U, B, R, G, C или комбинация, напр. WU>
RARITY: <common, uncommon, rare или mythic>
MANA_COST: <напр. {2}{R}{R} или {3}{U}{U}>
TYPE_LINE: <полная строка типа, напр. "Существо — Человек Маг">

Для CARD_TYPE: standard также добавь:
POWER: <сила, если creature, иначе пусто>
TOUGHNESS: <выносливость, если creature, иначе пусто>
RULES_TEXT: <правила; используй \\n для переносов строк>
FLAVOR_TEXT: <флavor-текст в кавычках или пусто>

Для CARD_TYPE: planeswalker также добавь:
STARTING_LOYALTY: <целое число>
ABILITY_1: <+N: текст способности>
ABILITY_2: <−N: текст способности>
ABILITY_3: <−N: текст способности (ультимейт)>
"""
