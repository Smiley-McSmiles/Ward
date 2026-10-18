"""Emoji helpers: :shortcode: conversion while typing, ASCII smileys, and emoji-only detection (shown large).

The emoji picker (gui/emoji_picker.py) uses GTK's own table of every emoji; this one only covers the shortcodes
people actually type (the GitHub/Slack names)."""

from __future__ import annotations

import re

SHORTCODES: dict[str, str] = {
    # faces
    "smile": "😄", "smiley": "😃", "grin": "😁", "grinning": "😀", "laughing": "😆", "satisfied": "😆",
    "sweat_smile": "😅", "joy": "😂", "rofl": "🤣", "slightly_smiling_face": "🙂", "upside_down_face": "🙃",
    "wink": "😉", "blush": "😊", "innocent": "😇", "smiling_face_with_three_hearts": "🥰", "heart_eyes": "😍",
    "star_struck": "🤩", "kissing_heart": "😘", "kissing": "😗", "relaxed": "☺️", "yum": "😋",
    "stuck_out_tongue": "😛", "stuck_out_tongue_winking_eye": "😜", "zany_face": "🤪",
    "stuck_out_tongue_closed_eyes": "😝", "money_mouth_face": "🤑", "hugs": "🤗", "hugging_face": "🤗",
    "hand_over_mouth": "🤭", "shushing_face": "🤫", "thinking": "🤔", "thinking_face": "🤔",
    "zipper_mouth_face": "🤐", "raised_eyebrow": "🤨", "neutral_face": "😐", "expressionless": "😑",
    "no_mouth": "😶", "smirk": "😏", "unamused": "😒", "roll_eyes": "🙄", "grimacing": "😬",
    "lying_face": "🤥", "relieved": "😌", "pensive": "😔", "sleepy": "😪", "drooling_face": "🤤",
    "sleeping": "😴", "mask": "😷", "face_with_thermometer": "🤒", "nauseated_face": "🤢", "vomiting_face": "🤮",
    "sneezing_face": "🤧", "hot_face": "🥵", "cold_face": "🥶", "woozy_face": "🥴", "dizzy_face": "😵",
    "exploding_head": "🤯", "cowboy_hat_face": "🤠", "partying_face": "🥳", "sunglasses": "😎", "nerd_face": "🤓",
    "monocle_face": "🧐", "confused": "😕", "worried": "😟", "slightly_frowning_face": "🙁", "frowning_face": "☹️",
    "open_mouth": "😮", "hushed": "😯", "astonished": "😲", "flushed": "😳", "pleading_face": "🥺",
    "frowning": "😦", "anguished": "😧", "fearful": "😨", "cold_sweat": "😰", "disappointed_relieved": "😥",
    "cry": "😢", "sob": "😭", "scream": "😱", "confounded": "😖", "persevere": "😣", "disappointed": "😞",
    "sweat": "😓", "weary": "😩", "tired_face": "😫", "yawning_face": "🥱", "triumph": "😤", "rage": "😡",
    "pout": "😡", "angry": "😠", "cursing_face": "🤬", "smiling_imp": "😈", "imp": "👿", "skull": "💀",
    "poop": "💩", "hankey": "💩", "clown_face": "🤡", "ghost": "👻", "alien": "👽", "robot": "🤖",
    "smiley_cat": "😺", "joy_cat": "😹", "heart_eyes_cat": "😻", "see_no_evil": "🙈", "hear_no_evil": "🙉",
    "speak_no_evil": "🙊", "melting_face": "🫠", "saluting_face": "🫡", "face_holding_back_tears": "🥹",
    # hands and people
    "+1": "👍", "thumbsup": "👍", "-1": "👎", "thumbsdown": "👎", "ok_hand": "👌", "pinched_fingers": "🤌",
    "v": "✌️", "crossed_fingers": "🤞", "love_you_gesture": "🤟", "metal": "🤘", "call_me_hand": "🤙",
    "point_left": "👈", "point_right": "👉", "point_up": "☝️", "point_up_2": "👆", "point_down": "👇",
    "middle_finger": "🖕", "raised_hand": "✋", "hand": "✋", "wave": "👋", "raised_back_of_hand": "🤚",
    "vulcan_salute": "🖖", "fist": "✊", "punch": "👊", "facepunch": "👊", "clap": "👏", "raised_hands": "🙌",
    "open_hands": "👐", "palms_up_together": "🤲", "handshake": "🤝", "pray": "🙏", "writing_hand": "✍️",
    "nail_care": "💅", "muscle": "💪", "eyes": "👀", "eye": "👁️", "brain": "🧠", "tongue": "👅",
    "facepalm": "🤦", "shrug": "🤷", "bow": "🙇", "raising_hand": "🙋", "ok_woman": "🙆", "no_good": "🙅",
    "dancer": "💃", "man_dancing": "🕺", "runner": "🏃", "walking": "🚶", "baby": "👶", "ninja": "🥷",
    # hearts and symbols
    "heart": "❤️", "orange_heart": "🧡", "yellow_heart": "💛", "green_heart": "💚", "blue_heart": "💙",
    "purple_heart": "💜", "black_heart": "🖤", "white_heart": "🤍", "brown_heart": "🤎", "broken_heart": "💔",
    "heart_on_fire": "❤️‍🔥", "two_hearts": "💕", "sparkling_heart": "💖", "heartpulse": "💗", "heartbeat": "💓",
    "revolving_hearts": "💞", "cupid": "💘", "kiss": "💋", "100": "💯", "anger": "💢", "boom": "💥",
    "collision": "💥", "dizzy": "💫", "sweat_drops": "💦", "dash": "💨", "zzz": "💤", "speech_balloon": "💬",
    "thought_balloon": "💭", "fire": "🔥", "sparkles": "✨", "star": "⭐", "star2": "🌟", "zap": "⚡",
    "white_check_mark": "✅", "heavy_check_mark": "✔️", "check": "✔️", "x": "❌", "negative_squared_cross_mark": "❎",
    "warning": "⚠️", "no_entry": "⛔", "no_entry_sign": "🚫", "question": "❓", "grey_question": "❔",
    "exclamation": "❗", "bangbang": "‼️", "interrobang": "⁉️", "heavy_plus_sign": "➕", "heavy_minus_sign": "➖",
    "arrow_right": "➡️", "arrow_left": "⬅️", "arrow_up": "⬆️", "arrow_down": "⬇️", "recycle": "♻️",
    "infinity": "♾️", "copyright": "©️", "registered": "®️", "tm": "™️", "lock": "🔒", "unlock": "🔓",
    "key": "🔑", "bell": "🔔", "no_bell": "🔕", "mag": "🔍", "link": "🔗", "pushpin": "📌", "paperclip": "📎",
    "memo": "📝", "pencil2": "✏️", "bulb": "💡", "calendar": "📅", "chart_with_upwards_trend": "📈",
    "clipboard": "📋", "email": "📧", "envelope": "✉️", "package": "📦", "phone": "📱", "iphone": "📱",
    "computer": "💻", "keyboard": "⌨️", "camera": "📷", "movie_camera": "🎥", "tv": "📺", "headphones": "🎧",
    "musical_note": "🎵", "notes": "🎶", "microphone": "🎤", "loudspeaker": "📢", "mega": "📣", "hourglass": "⌛",
    "alarm_clock": "⏰", "stopwatch": "⏱️", "moneybag": "💰", "gem": "💎", "wrench": "🔧", "hammer": "🔨",
    "gear": "⚙️", "shield": "🛡️", "crossed_swords": "⚔️", "bomb": "💣", "pill": "💊", "trophy": "🏆",
    "medal_sports": "🏅", "1st_place_medal": "🥇", "crown": "👑", "gift": "🎁", "tada": "🎉", "confetti_ball": "🎊",
    "balloon": "🎈", "birthday": "🎂", "rocket": "🚀", "airplane": "✈️", "car": "🚗", "bike": "🚲",
    "house": "🏠", "office": "🏢", "hospital": "🏥", "flag_white": "🏳️", "rainbow_flag": "🏳️‍🌈",
    "checkered_flag": "🏁", "triangular_flag_on_post": "🚩", "video_game": "🎮", "dart": "🎯", "game_die": "🎲",
    "jigsaw": "🧩", "soccer": "⚽", "basketball": "🏀", "books": "📚", "book": "📖", "mortar_board": "🎓",
    # nature and food
    "sunny": "☀️", "sun": "☀️", "cloud": "☁️", "partly_sunny": "⛅", "rainbow": "🌈", "umbrella": "☔",
    "snowflake": "❄️", "snowman": "⛄", "cyclone": "🌀", "ocean": "🌊", "earth_africa": "🌍",
    "earth_americas": "🌎", "earth_asia": "🌏", "globe_with_meridians": "🌐", "moon": "🌙", "crescent_moon": "🌙",
    "full_moon": "🌕", "new_moon": "🌑", "seedling": "🌱", "herb": "🌿", "four_leaf_clover": "🍀",
    "maple_leaf": "🍁", "fallen_leaf": "🍂", "rose": "🌹", "sunflower": "🌻", "tulip": "🌷", "cherry_blossom": "🌸",
    "cactus": "🌵", "palm_tree": "🌴", "evergreen_tree": "🌲", "christmas_tree": "🎄", "mushroom": "🍄",
    "dog": "🐶", "cat": "🐱", "mouse": "🐭", "hamster": "🐹", "rabbit": "🐰", "fox_face": "🦊", "bear": "🐻",
    "panda_face": "🐼", "koala": "🐨", "tiger": "🐯", "lion": "🦁", "cow": "🐮", "pig": "🐷", "frog": "🐸",
    "monkey_face": "🐵", "chicken": "🐔", "penguin": "🐧", "bird": "🐦", "eagle": "🦅", "owl": "🦉",
    "bat": "🦇", "wolf": "🐺", "horse": "🐴", "unicorn": "🦄", "bee": "🐝", "bug": "🐛", "butterfly": "🦋",
    "snail": "🐌", "turtle": "🐢", "snake": "🐍", "dragon": "🐉", "octopus": "🐙", "fish": "🐟",
    "dolphin": "🐬", "whale": "🐳", "shark": "🦈", "crab": "🦀", "t-rex": "🦖", "sloth": "🦥",
    "apple": "🍎", "green_apple": "🍏", "pear": "🍐", "tangerine": "🍊", "lemon": "🍋", "banana": "🍌",
    "watermelon": "🍉", "grapes": "🍇", "strawberry": "🍓", "cherries": "🍒", "peach": "🍑", "pineapple": "🍍",
    "avocado": "🥑", "eggplant": "🍆", "carrot": "🥕", "corn": "🌽", "hot_pepper": "🌶️", "bread": "🍞",
    "cheese": "🧀", "egg": "🥚", "bacon": "🥓", "hamburger": "🍔", "fries": "🍟", "pizza": "🍕",
    "hotdog": "🌭", "taco": "🌮", "burrito": "🌯", "popcorn": "🍿", "spaghetti": "🍝", "ramen": "🍜",
    "sushi": "🍣", "rice": "🍚", "curry": "🍛", "cookie": "🍪", "cake": "🍰", "doughnut": "🍩", "candy": "🍬",
    "chocolate_bar": "🍫", "ice_cream": "🍨", "coffee": "☕", "tea": "🍵", "beer": "🍺", "beers": "🍻",
    "wine_glass": "🍷", "cocktail": "🍸", "tropical_drink": "🍹", "champagne": "🍾", "clinking_glasses": "🥂",
    "milk_glass": "🥛",
}

SMILEYS: dict[str, str] = {
    ":)": "🙂", ":-)": "🙂", ":D": "😃", ":-D": "😃", ";)": "😉", ";-)": "😉", ":(": "🙁", ":-(": "🙁",
    ":P": "😛", ":-P": "😛", ":p": "😛", ":O": "😮", ":o": "😮", ":'(": "😢", "<3": "❤️", "</3": "💔",
    ":|": "😐", ":-|": "😐", "B)": "😎", "8)": "😎", ":*": "😘", ":/": "😕", "XD": "😆", "xD": "😆",
    "^^": "😊", "o/": "👋", ":3": "😺",
}

SHORTCODE_RE = re.compile(r":([a-z0-9_+\-]+):")
_SMILEY_RE = re.compile(r"(?:(?<=\s)|^)(%s)(?=\s|$)" % "|".join(re.escape(k) for k in
                                                                  sorted(SMILEYS, key=len, reverse=True)))


def replace_shortcodes(text: str) -> str:
    return SHORTCODE_RE.sub(lambda m: SHORTCODES.get(m.group(1), m.group(0)), text)


def replace_smileys(text: str) -> str:
    # Never inside URLs or code: only whole whitespace-separated tokens are replaced
    return _SMILEY_RE.sub(lambda m: SMILEYS[m.group(1)], text)


def complete(prefix: str, limit: int = 8) -> list[tuple[str, str]]:
    """Shortcode completions for ':pre' -> [(name, emoji)], exact prefix matches first."""
    p = prefix.lower().lstrip(":")
    if len(p) < 2:
        return []
    starts = sorted((k, v) for k, v in SHORTCODES.items() if k.startswith(p))
    contains = sorted((k, v) for k, v in SHORTCODES.items() if p in k and not k.startswith(p))
    seen, out = set(), []
    for k, v in starts + contains:
        if v in seen:
            continue
        seen.add(v)
        out.append((k, v))
        if len(out) >= limit:
            break
    return out


# One emoji "cluster": a pictograph with optional variation selector, skin tone and tag sequence, joined by ZWJ
# to further pictographs; or a flag (two regional indicators); or a keycap.
_PICTO = (r"[©®‼⁉™ℹ↔-↙↩↪⌚⌛⌨⏏⏩-⏳"
          r"⏸-⏺Ⓜ▪▫▶◀◻-◾☀-➿⤴⤵⬅-⬇"
          r"⬛⬜⭐⭕〰〽㊗㊙\U0001f000-\U0001f1e5\U0001f201-\U0001faff]")
_MOD = r"[️︎\U0001f3fb-\U0001f3ff\U000e0020-\U000e007f]*"
_CLUSTER = re.compile(r"(?:%s%s(?:‍%s%s)*|[\U0001f1e6-\U0001f1ff]{2}|[0-9#*]️?⃣)"
                      % (_PICTO, _MOD, _PICTO, _MOD))


def emoji_only(text: str, limit: int = 3) -> int:
    """Number of emoji when the text is nothing but 1..limit emoji (and spaces), else 0."""
    s = (text or "").strip()
    if not s or len(s) > 64:
        return 0
    count = 0
    pos = 0
    while pos < len(s):
        if s[pos].isspace():
            pos += 1
            continue
        m = _CLUSTER.match(s, pos)
        if not m or m.end() == pos:
            return 0
        count += 1
        if count > limit:
            return 0
        pos = m.end()
    return count
