import os
from dotenv import load_dotenv
import json
import logging
import datetime
from datetime import timezone, timedelta
import discord
from discord.ext import commands, tasks

# ---------------------------------------------
# KONFIGURACJA LOGOWANIA I STAŁE
# ---------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s"
)

BOT_PREFIX = "-"
DATA_FILE = "data.json"
NBSP = "\u00A0"  # non-breaking space separator

# ---------------------------------------------
# INTENTS i PARTIALS (dla poprawnej obsługi reakcji)
# ---------------------------------------------
intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
intents.members = True

bot = commands.Bot(
    command_prefix=BOT_PREFIX,
    intents=intents,
    help_command=None,
    partials=["MESSAGE", "REACTION", "USER"]
)

# ---------------------------------------------
# DEFINICJA UŻYWEK – EDYCJA W JEDNYM MIEJSCU
# ---------------------------------------------
SUBSTANCES = {
    "piwo": {"emoji": "🍺", "ethanol_grams": 19.73, "duration_hours": 3},
    "wodka": {"emoji": "🍸", "ethanol_grams": 15.78, "duration_hours": 2},
    "whiskey": {"emoji": "🥃", "ethanol_grams": 31.56, "duration_hours": 2},
    "wino": {"emoji": "🍷", "ethanol_grams": 23.67, "duration_hours": 2},
    "drink": {"emoji": "🍹", "ethanol_grams": 23.23, "duration_hours": 2},
    "likier": {"emoji": "🍶", "ethanol_grams": 18.50, "duration_hours": 2},
    "blunt": {"emoji": "🍃", "ethanol_grams": 0, "duration_hours": 4}
}
VALID_TYPES = set(SUBSTANCES.keys())
EMOJI_TO_TYPE = {data["emoji"]: typ for typ, data in SUBSTANCES.items()}
TYPE_TO_EMOJI = {typ: data["emoji"] for typ, data in SUBSTANCES.items()}

# ---------------------------------------------
# GLOBALNE DANE: Struktura bazy
# ---------------------------------------------
guild_data = {}


# ---------------------------------------------
# FUNKCJE ZAPISU I ODCZYTU DANYCH
# ---------------------------------------------
def load_data():
    global guild_data
    if not os.path.exists(DATA_FILE):
        guild_data = {"guilds": {}}
        save_data()
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            guild_data = json.load(f)
    except (json.JSONDecodeError, OSError):
        logging.error("Błąd odczytu pliku JSON – tworzenie nowego pliku...")
        guild_data = {"guilds": {}}
        save_data()


def save_data():
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(guild_data, f, ensure_ascii=False, indent=2)
        logging.info("Dane zapisano do pliku.")
    except OSError as e:
        logging.error(f"Błąd zapisu pliku JSON: {e}")


# ---------------------------------------------
# FUNKCJE POMOCNICZE: Ustawienia i użytkownicy dla gildii
# ---------------------------------------------
def get_guild_settings(guild: discord.Guild):
    gid = str(guild.id)
    if gid not in guild_data:
        guild_data[gid] = {"settings": {}, "users": {}}
    return guild_data[gid]["settings"]


def get_guild_users(guild: discord.Guild):
    gid = str(guild.id)
    if gid not in guild_data:
        guild_data[gid] = {"settings": {}, "users": {}}
    return guild_data[gid]["users"]


def get_current_month():
    return datetime.datetime.now(timezone.utc).strftime("%Y-%m")


def create_new_user(nick: str):
    return {
        "original_nick": nick,
        "consumptions": {typ: [] for typ in VALID_TYPES},
        "monthly_usage": {},  # Format: {"YYYY-MM": {typ: aggregated_count}}
        "weight": 80.0,
        "display_mode": "promile"
    }


# ---------------------------------------------
# FUNKCJA: USUWANIE NBSP z nicku
# ---------------------------------------------
# def remove_bot_suffix(nick: str) -> str:
#     if not nick:
#         return nick
#     idx = nick.find(NBSP)
#     if idx != -1:
#         return nick[:idx]
#     return nick


# ---------------------------------------------
# POMOCNICZA FUNKCJA: Pobranie obiektu Member
# ---------------------------------------------
async def get_member(guild: discord.Guild, user_id: int) -> discord.Member:
    member = guild.get_member(user_id)
    if member is None:
        try:
            member = await guild.fetch_member(user_id)
        except discord.NotFound:
            member = None
    return member


# ---------------------------------------------
# PRUNING: Usuwanie przeterminowanych zdarzeń spożycia
# ---------------------------------------------
def prune_consumptions(data: dict, weight: float):
    r = 0.68
    now = datetime.datetime.now(timezone.utc)
    new_consumptions = {}
    for typ, events in data.get("consumptions", {}).items():
        new_events = []
        for event in events:
            try:
                event_time = datetime.datetime.fromisoformat(event["timestamp"])
            except Exception:
                continue
            hours_elapsed = (now - event_time).total_seconds() / 3600.0
            if typ == "blunt":
                if hours_elapsed < SUBSTANCES[typ]["duration_hours"]:
                    new_events.append(event)
            else:
                dose = event.get("dose", 0)
                base_bac = (dose * SUBSTANCES[typ]["ethanol_grams"]) / (weight * 1000 * r) * 1000
                current_bac = base_bac - 0.15 * hours_elapsed
                if current_bac > 0:
                    new_events.append(event)
        if new_events:
            new_consumptions[typ] = new_events
    data["consumptions"] = new_consumptions


# ---------------------------------------------
# OBLICZANIE PROMILI (BAC) Z METABOLIZMEM
# ---------------------------------------------
def compute_bac(data: dict, weight: float) -> float:
    elimination_rate = 0.15  # promile na godzinę
    total_bac = 0.0
    now = datetime.datetime.now(timezone.utc)
    r = 0.68  # stała dystrybucji
    for typ in VALID_TYPES:
        if typ == "blunt":
            continue
        events = data.get("consumptions", {}).get(typ, [])
        for event in events:
            try:
                event_time = datetime.datetime.fromisoformat(event["timestamp"])
            except Exception:
                continue
            hours_elapsed = (now - event_time).total_seconds() / 3600.0
            base_bac = (event["dose"] * SUBSTANCES[typ]["ethanol_grams"]) / (weight * 1000 * r) * 1000
            current_bac = base_bac - elimination_rate * hours_elapsed
            if current_bac < 0:
                current_bac = 0
            total_bac += current_bac
    return total_bac


# ---------------------------------------------
# BUDOWANIE CIĄGU STATUSU (do nicku)
# ---------------------------------------------
# def build_usage_string(data: dict) -> str:
#     total_alcohol = sum(
#         sum(event["dose"] for event in data.get("consumptions", {}).get(typ, []))
#         for typ in VALID_TYPES if typ != "blunt"
#     )
#     blunt_total = sum(event["dose"] for event in data.get("consumptions", {}).get("blunt", []))
#     if total_alcohol == 0 and blunt_total == 0:
#         return ""
#     mode = data.get("display_mode", "promile")
#     if mode == "promile":
#         if total_alcohol > 0:
#             bac = compute_bac(data, data.get("weight", 80.0))
#             if blunt_total > 0:
#                 return f"{bac:.2f}‰ {TYPE_TO_EMOJI['blunt']}{blunt_total}"
#             else:
#                 return f"{bac:.2f}‰"
#         else:
#             return f"{TYPE_TO_EMOJI['blunt']}{blunt_total}"
#     else:
#         monthly = data.get("monthly_usage", {}).get(get_current_month(), {})
#         parts = [f"{TYPE_TO_EMOJI[typ]}{monthly.get(typ, 0)}" for typ in VALID_TYPES if monthly.get(typ, 0) > 0]
#         return "".join(parts)


# ---------------------------------------------
# AKTUALIZACJA NICKU UŻYTKOWNIKA
# ---------------------------------------------
# async def update_nickname(member: discord.Member):
#     users = get_guild_users(member.guild)
#     data = users.get(str(member.id))
#     if not data:
#         return
#
#     # Prune zdarzeń spożycia przed obliczeniem nicku
#     prune_consumptions(data, data.get("weight", 80.0))
#
#     current_nick = member.nick or member.name
#     original_nick = data["original_nick"]
#     if original_nick not in current_nick and NBSP not in current_nick:
#         data["original_nick"] = current_nick
#         original_nick = current_nick
#     usage_str = build_usage_string(data)
#     new_nick = f"{original_nick}{NBSP}{usage_str}" if usage_str else original_nick
#     if len(new_nick) > 32:
#         new_nick = new_nick[:31] + "…"
#     if new_nick != current_nick:
#         try:
#             await member.edit(nick=new_nick)
#             logging.info(f"Zmieniono nick użytkownika {member.name} na {new_nick}")
#         except discord.Forbidden:
#             logging.error(f"Brak uprawnień do zmiany nicku użytkownika {member.name}")
#         except Exception as e:
#             logging.error(f"Błąd przy zmianie nicku {member.name}: {e}")


# ---------------------------------------------
# INICJALIZACJA WIADOMOŚCI STATUSOWEJ (HELPER)
# ---------------------------------------------
async def init_status_message_helper(guild: discord.Guild, channel: discord.TextChannel) -> None:
    settings = get_guild_settings(guild)
    try:
        msg_id = await send_status_message(guild, channel)
        settings["status_message_id"] = msg_id
        save_data()
        logging.info(f"Wiadomość statusowa wysłana na {guild.name}")
    except (discord.Forbidden, discord.HTTPException) as e:
        logging.error(f"Nie udało się zainicjować wiadomości statusowej na {guild.name}: {e}")


# ---------------------------------------------
# INICJALIZACJA LEADERBOARDU MIESIĘCZNEGO (HELPER)
# ---------------------------------------------
async def init_leaderboard_helper(guild: discord.Guild, channel: discord.TextChannel) -> None:
    settings = get_guild_settings(guild)
    try:
        embed = build_leaderboard_embed(guild)
        msg = await channel.send(embed=embed)
        settings["live_leaderboard_message_id"] = msg.id
        settings["live_leaderboard_channel_id"] = channel.id
        save_data()
        logging.info(f"Leaderboard miesięczny wysłany na {guild.name}")
    except (discord.Forbidden, discord.HTTPException) as e:
        logging.error(f"Nie udało się zainicjować leaderboardu na {guild.name}: {e}")


# ---------------------------------------------
# INICJALIZACJA LEADERBOARDU PROMILOWEGO (HELPER)
# ---------------------------------------------
async def init_bac_leaderboard_helper(guild: discord.Guild, channel: discord.TextChannel) -> None:
    settings = get_guild_settings(guild)
    try:
        embed = build_bac_leaderboard_embed(guild)
        msg = await channel.send(embed=embed)
        settings["bac_leaderboard_message_id"] = msg.id
        settings["bac_leaderboard_channel_id"] = channel.id
        save_data()
        logging.info(f"Leaderboard promilowy wysłany na {guild.name}")
    except (discord.Forbidden, discord.HTTPException) as e:
        logging.error(f"Nie udało się zainicjować leaderboardu promilowego na {guild.name}: {e}")


# ---------------------------------------------
# WYSYŁANIE WIADOMOŚCI STATUSOWEJ
# ---------------------------------------------
async def send_status_message(guild: discord.Guild, channel: discord.TextChannel) -> int:
    status_text = "**Kliknij w reakcję, aby dodać spożycie**:\n"
    for typ, data in SUBSTANCES.items():
        line = f"{data['emoji']} — {typ.capitalize()} - ({data['duration_hours']}h, ~{data['ethanol_grams']:.1f}g etanolu)\n"
        if len(status_text) + len(line) > 2000:
            logging.warning(f"Limit długości wiadomości przekroczony na {guild.name}")
            break
        status_text += line
    status_text += "❌ — Wyczyść status"
    try:
        msg = await channel.send(status_text)
        for emoji in EMOJI_TO_TYPE:
            await msg.add_reaction(emoji)
        await msg.add_reaction("❌")
        return msg.id
    except (discord.Forbidden, discord.HTTPException) as e:
        logging.error(f"Błąd podczas wysyłania wiadomości na {guild.name}: {e}")
    return 0


# ---------------------------------------------
# BUDOWANIE EMBEDU LEADERBOARDU MIESIĘCZNEGO
# ---------------------------------------------
def build_leaderboard_embed(guild: discord.Guild) -> discord.Embed:
    current_month = get_current_month()
    users = get_guild_users(guild)
    usage_list = []
    # Zbieramy dane użytkowników, którzy mają przynajmniej jedną używkę (czyli count > 0) w bieżącym miesiącu
    for user_id, data in users.items():
        monthly = data.get("monthly_usage", {}).get(current_month, {})
        # Obliczamy łączną gramaturę etanolu – iterujemy po wszystkich używkach z SUBSTANCES
        total_grams = sum(monthly.get(typ, 0) * SUBSTANCES[typ]["ethanol_grams"] for typ in SUBSTANCES)
        if any(monthly.get(typ, 0) > 0 for typ in SUBSTANCES):
            usage_list.append((user_id, data, monthly, total_grams))
    # Sortujemy malejąco wg łącznej gramatury etanolu (użytkownicy z samymi bluntami będą mieli 0)
    usage_list.sort(key=lambda x: x[3], reverse=True)

    embed = discord.Embed(
        title=f"Tabela wyników (miesięczna) – {current_month}",
        color=discord.Color.green()
    )
    if not usage_list:
        embed.description = "Brak aktywności w tym miesiącu."
    else:
        for pos, (user_id, data, monthly, total_grams) in enumerate(usage_list, start=1):
            # Używamy oryginalnego nicku, zapisanego w bazie, aby leaderboard był "czysty"
            name = data.get("original_nick")
            if not name:
                member_obj = discord.utils.get(guild.members, id=int(user_id))
                name = member_obj.display_name if member_obj else f"<@{user_id}>"
            details = []
            for typ in SUBSTANCES:
                count = monthly.get(typ, 0)
                if count == 0:
                    continue  # pomijamy puste pozycje
                if typ == "blunt":
                    details.append(f"{TYPE_TO_EMOJI[typ]}{count}")
                else:
                    grams = count * SUBSTANCES[typ]["ethanol_grams"]
                    details.append(f"{TYPE_TO_EMOJI[typ]}{count} ({grams:.1f}g)")
            details_str = " ".join(details)
            drink_count = monthly.get("drink", 0)
            embed.add_field(
                name=f"{pos}. {name}",
                value=f"{details_str}\nSuma etanolu: {total_grams:.1f}",
                inline=False
            )
    return embed


# ---------------------------------------------
# BUDOWANIE EMBEDU LEADERBOARDU PROMILOWEGO
# ---------------------------------------------
def build_bac_leaderboard_embed(guild: discord.Guild) -> discord.Embed:
    users = get_guild_users(guild)
    bac_list = []
    for user_id, data in users.items():
        bac = compute_bac(data, data.get("weight", 80.0))
        if bac > 0:
            bac_list.append((user_id, bac, data))
    bac_list.sort(key=lambda x: x[1], reverse=True)
    embed = discord.Embed(
        title="Leaderboard (promile) – aktualnie",
        color=discord.Color.blue()
    )
    if not bac_list:
        embed.description = "Brak aktywności."
    else:
        for pos, (user_id, bac, data) in enumerate(bac_list, start=1):
            name = data.get("original_nick")
            if not name:
                member_obj = discord.utils.get(guild.members, id=int(user_id))
                name = member_obj.display_name if member_obj else f"<@{user_id}>"
            embed.add_field(name=f"{pos}. {name}", value=f"{bac:.2f}‰", inline=False)
    return embed


# ---------------------------------------------
# INICJALIZACJA LEADERBOARDÓW
# ---------------------------------------------
async def init_leaderboard(guild: discord.Guild, channel: discord.TextChannel) -> None:
    await init_leaderboard_helper(guild, channel)


async def init_bac_leaderboard(guild: discord.Guild, channel: discord.TextChannel) -> None:
    await init_bac_leaderboard_helper(guild, channel)


# ---------------------------------------------
# ZAPLANOWANE ZADANIE: AKTUALIZACJA LEADERBOARDÓW CO MINUTĘ
# ---------------------------------------------
@tasks.loop(minutes=1)
async def update_tasks():
    for guild in bot.guilds:
        settings = get_guild_settings(guild)
        lb_channel_id = settings.get("live_leaderboard_channel_id")
        lb_message_id = settings.get("live_leaderboard_message_id")
        if lb_channel_id is None or lb_message_id is None:
            continue
        channel = guild.get_channel(lb_channel_id)
        if channel:
            try:
                msg = await channel.fetch_message(lb_message_id)
                embed = build_leaderboard_embed(guild)
                await msg.edit(embed=embed)
            except discord.NotFound:
                logging.warning(f"Miesięczny leaderboard nie znaleziono na {guild.name}, regeneruję...")
                await init_leaderboard(guild, channel)
            except (discord.Forbidden, discord.HTTPException) as e:
                logging.error(f"Błąd przy aktualizacji miesięcznego leaderboardu: {e}")
        bac_lb_channel_id = settings.get("bac_leaderboard_channel_id")
        bac_lb_message_id = settings.get("bac_leaderboard_message_id")
        if bac_lb_channel_id and bac_lb_message_id:
            channel_bac = guild.get_channel(bac_lb_channel_id)
            if channel_bac:
                try:
                    msg = await channel_bac.fetch_message(bac_lb_message_id)
                    embed = build_bac_leaderboard_embed(guild)
                    await msg.edit(embed=embed)
                except discord.NotFound:
                    logging.warning(f"Promilowy leaderboard nie znaleziono na {guild.name}, regeneruję...")
                    await init_bac_leaderboard(guild, channel_bac)
                except (discord.Forbidden, discord.HTTPException) as e:
                    logging.error(f"Błąd przy aktualizacji promilowego leaderboardu: {e}")


# ---------------------------------------------
# ZAPLANOWANE ZADANIE: AKTUALIZACJA NICKÓW WSZYSTKICH UŻYTKOWNIKÓW CO MINUTĘ
# ---------------------------------------------
# @tasks.loop(minutes=1)
# async def update_all_nicknames():
#     for guild in bot.guilds:
#         users = get_guild_users(guild)
#         for user_id in list(users.keys()):
#             member = await get_member(guild, int(user_id))
#             # if member:
#                 # await update_nickname(member)
#     save_data()
#     logging.info("Zaktualizowano nicki wszystkich użytkowników.")


# ---------------------------------------------
# KOMENDA: HELPME (pomoc)
# ---------------------------------------------
@bot.command(name="helpme")
async def helpme_cmd(ctx):
    help_text = (
        f"**Komendy (prefiks: {BOT_PREFIX})**:\n"
        f"{BOT_PREFIX}helpme – Wyświetla tę pomoc\n"
        f"{BOT_PREFIX}add <typ> <ilość> – Dodaje spożycie do Twojego statusu\n"
        f"{BOT_PREFIX}add <nick> <typ> <ilość> – Dodaje spożycie do cudzego statusu (Admin)\n"
        f"{BOT_PREFIX}status – Wyświetla Twój status wraz z aktualnymi promilami\n"
        f"{BOT_PREFIX}clear [<nick>] – Czyści status (Admin opcjonalnie)\n"
        f"{BOT_PREFIX}leaderboard – Wyświetla tabelę wyników miesięcznych\n"
        f"{BOT_PREFIX}leaderboard_promile – Wyświetla ranking aktualnych promili\n"
        f"{BOT_PREFIX}init_status_message – Tworzy wiadomość z reakcjami\n"
        f"{BOT_PREFIX}setchannel <kanał> – Ustawia kanał nasłuchu (Admin)\n"
        f"{BOT_PREFIX}live_leaderboard – Wysyła embed leaderboard miesięczny (Admin)\n"
        f"{BOT_PREFIX}setdedicatedchannel <kanał> – Ustawia dedykowany kanał (Admin)\n"
        f"{BOT_PREFIX}setweight <kg> – Zmienia Twoją wagę (domyślnie 80kg)\n"
        f"{BOT_PREFIX}setmode <promile|emoji> – Wybiera tryb wyświetlania w nicku\n"
        f"{BOT_PREFIX}shutdown – Bezpieczne wyłączenie bota (Admin)\n"
        f"{BOT_PREFIX}ping – Odpowiada 'Pong!'\n"
    )
    await ctx.send(help_text)


# ---------------------------------------------
# KOMENDA: INIT_STATUS_MESSAGE
# ---------------------------------------------
@bot.command()
async def init_status_message(ctx):
    settings = get_guild_settings(ctx.guild)
    channel = ctx.guild.get_channel(settings.get("dedicated_channel_id")) or ctx.channel
    msg_id = await send_status_message(ctx.guild, channel)
    settings["status_message_id"] = msg_id
    save_data()
    await ctx.send("Wiadomość z reakcjami została utworzona.")


# ---------------------------------------------
# KOMENDA: SETDEDICATEDCHANNEL
# ---------------------------------------------
@bot.command()
async def setdedicatedchannel(ctx, channel: discord.TextChannel):
    settings = get_guild_settings(ctx.guild)
    settings["dedicated_channel_id"] = channel.id
    save_data()
    await ctx.send(f"Dedykowany kanał ustawiony na {channel.mention}.")


# ---------------------------------------------
# KOMENDA: SETCHANNEL
# ---------------------------------------------
@bot.command()
async def setchannel(ctx, channel: discord.TextChannel):
    settings = get_guild_settings(ctx.guild)
    settings["listening_channel_id"] = channel.id
    save_data()
    await ctx.send(f"Kanał nasłuchu ustawiony na {channel.mention}.")


# ---------------------------------------------
# KOMENDA: LIVE_LEADERBOARD (miesięczny)
# ---------------------------------------------
@bot.command(name="live_leaderboard")
async def live_leaderboard_cmd(ctx):
    settings = get_guild_settings(ctx.guild)
    channel = ctx.guild.get_channel(settings.get("dedicated_channel_id")) or ctx.channel
    await init_leaderboard_helper(ctx.guild, channel)
    await ctx.send("Leaderboard miesięczny został wysłany i będzie aktualizowany.")


# ---------------------------------------------
# KOMENDA: SETWEIGHT
# ---------------------------------------------
@bot.command()
async def setweight(ctx, weight: float):
    users = get_guild_users(ctx.guild)
    user_id = str(ctx.author.id)
    if user_id not in users:
        users[user_id] = create_new_user(ctx.author.name)
    users[user_id]["weight"] = weight
    save_data()
    try:
        await ctx.message.delete()
    except Exception as e:
        logging.warning(f"Nie udało się usunąć wiadomości: {e}")
    try:
        await ctx.author.send(f"Twoja waga została ustawiona na {weight} kg.")
    except discord.Forbidden:
        logging.warning(f"Nie udało się wysłać DM do użytkownika {ctx.author.name}.")


# ---------------------------------------------
# KOMENDA: SETMODE
# ---------------------------------------------
@bot.command()
async def setmode(ctx, mode: str):
    mode = mode.lower()
    if mode not in ("promile", "emoji"):
        await ctx.send("Tryb musi być 'promile' lub 'emoji'.")
        return
    users = get_guild_users(ctx.guild)
    user_id = str(ctx.author.id)
    if user_id not in users:
        users[user_id] = create_new_user(ctx.author.name)
    users[user_id]["display_mode"] = mode
    save_data()
    try:
        await ctx.author.send(f"Tryb wyświetlania został ustawiony na {mode}.")
    except Exception:
        await ctx.send("Tryb wyświetlania został ustawiony (prywatnie).")


# ---------------------------------------------
# KOMENDA: STATUS
# ---------------------------------------------
@bot.command()
async def status(ctx):
    users = get_guild_users(ctx.guild)
    data = users.get(str(ctx.author.id))
    if not data:
        await ctx.send("Nie masz żadnego statusu.")
        return
    month = get_current_month()
    monthly = data.get("monthly_usage", {}).get(month, {})
    lines = [f"• {typ.capitalize()}: {monthly.get(typ, 0)}" for typ in VALID_TYPES if monthly.get(typ, 0) > 0]
    current_bac = compute_bac(data, data.get("weight", 80.0))
    lines.append(f"• Aktualne promile: {current_bac:.2f}‰")
    await ctx.send("**Twój status**:\n" + "\n".join(lines))


# ---------------------------------------------
# KOMENDA: CLEAR
# ---------------------------------------------
@bot.command()
async def clear(ctx, user_arg: str = None):
    users = get_guild_users(ctx.guild)
    if not user_arg:
        user_id = str(ctx.author.id)
        if user_id not in users:
            await ctx.send("Nie masz statusu do wyczyszczenia.")
            return
        del users[user_id]
        # try:
        #     member = ctx.author
        #     original = remove_bot_suffix(member.nick) if member.nick else member.name
        #     await member.edit(nick=original)
        # except Exception as e:
        #     logging.warning(f"Nie udało się przywrócić nicku: {e}")
        await ctx.send("Twój status został wyczyszczony.")
        save_data()
    else:
        if not ctx.author.guild_permissions.manage_nicknames:
            return
        member = discord.utils.find(lambda m: m.name == user_arg or (m.nick and m.nick == user_arg), ctx.guild.members)
        if not member:
            await ctx.send(f"Nie znaleziono użytkownika: {user_arg}")
            return
        user_id = str(member.id)
        if user_id not in users:
            await ctx.send(f"Użytkownik {member.mention} nie ma statusu.")
            return
        del users[user_id]
        # try:
        #     original = remove_bot_suffix(member.nick) if member.nick else member.name
        #     await member.edit(nick=original)
        # except Exception as e:
        #     logging.warning(f"Nie udało się przywrócić nicku dla {member.name}: {e}")
        await ctx.send(f"Status użytkownika {member.mention} wyczyszczony.")
        save_data()


# ---------------------------------------------
# KOMENDA: LEADERBOARD (miesięczny)
# ---------------------------------------------
@bot.command(name="leaderboard")
async def leaderboard_cmd(ctx, hide_arg: str = None):
    users = get_guild_users(ctx.guild)
    current_month = get_current_month()
    usage_list = []
    for user_id, data in users.items():
        monthly = data.get("monthly_usage", {}).get(current_month, {})
        total = sum(monthly.get(typ, 0) for typ in VALID_TYPES)
        if total > 0:
            usage_list.append((user_id, data, total))
    usage_list.sort(key=lambda x: x[2], reverse=True)
    if not usage_list:
        text = f"Nikt nie ma punktów w miesiącu {current_month}."
    else:
        lines = []
        for pos, (user_id, data, total) in enumerate(usage_list, start=1):
            name = data.get("original_nick")
            if not name:
                member_obj = await get_member(ctx.guild, int(user_id))
                name = member_obj.display_name if member_obj else f"<@{user_id}>"
            lines.append(f"**{pos}. {name}** – Suma: {total}")
        text = "\n".join(lines)
    if hide_arg == "hide":
        try:
            await ctx.author.send(text)
            await ctx.send("Sprawdź DM.")
        except discord.Forbidden:
            await ctx.send("Nie mogę wysłać DM.")
    else:
        await ctx.send(text)


# ---------------------------------------------
# KOMENDA: LEADERBOARD_PROMILE
# ---------------------------------------------
@bot.command(name="leaderboard_promile")
async def leaderboard_promile_cmd(ctx):
    users = get_guild_users(ctx.guild)
    bac_list = []
    for user_id, data in users.items():
        bac = compute_bac(data, data.get("weight", 80.0))
        if bac > 0:
            bac_list.append((user_id, bac, data))
    bac_list.sort(key=lambda x: x[1], reverse=True)
    if not bac_list:
        text = "Nikt nie ma aktualnie promili."
    else:
        lines = []
        for pos, (user_id, bac, data) in enumerate(bac_list, start=1):
            name = data.get("original_nick")
            if not name:
                member_obj = await get_member(ctx.guild, int(user_id))
                name = member_obj.display_name if member_obj else f"<@{user_id}>"
            lines.append(f"**{pos}. {name}** – {bac:.2f}‰")
        text = "\n".join(lines)
    await ctx.send(text)


# ---------------------------------------------
# KOMENDA: PING
# ---------------------------------------------
@bot.command(name="ping")
async def ping_cmd(ctx):
    await ctx.send("Pong!")


# ---------------------------------------------
# EVENT: on_reaction_add – OBSŁUGA REAKCJI W WIADOMOŚCI STATUSOWEJ
# ---------------------------------------------
@bot.event
async def on_reaction_add(reaction, user):
    if user.bot:
        return
    message = reaction.message
    if not message.guild:
        return
    settings = get_guild_settings(message.guild)
    if message.id != settings.get("status_message_id"):
        return
    emoji = str(reaction.emoji)
    if emoji in EMOJI_TO_TYPE:
        typ = EMOJI_TO_TYPE[emoji]
        users = get_guild_users(message.guild)
        user_id = str(user.id)
        if user_id not in users:
            users[user_id] = create_new_user(user.name)
        data = users[user_id]
        now = datetime.datetime.now(timezone.utc)
        event = {"dose": 1, "timestamp": now.isoformat()}
        data.setdefault("consumptions", {}).setdefault(typ, []).append(event)
        month = get_current_month()
        if month not in data.get("monthly_usage", {}):
            data["monthly_usage"][month] = {t: 0 for t in VALID_TYPES}
        data["monthly_usage"][month][typ] += 1
        member_obj = await get_member(message.guild, user.id)
        # if member_obj:
        #     await update_nickname(member_obj)
        #     if member_obj.guild_permissions.manage_nicknames:
        #         try:
        #             await member_obj.send(f"Twój nowy nick to: {member_obj.nick}")
        #         except discord.Forbidden:
        #             logging.warning(f"Nie udało się wysłać wiadomości do admina {member_obj.name}")
        # save_data()
        try:
            await message.remove_reaction(emoji, user)
        except Exception as e:
            logging.warning(f"Nie udało się usunąć reakcji: {e}")
    elif emoji == "❌":
        users = get_guild_users(message.guild)
        user_id = str(user.id)
        if user_id in users:
            del users[user_id]
        member_obj = await get_member(message.guild, user.id)
        # if member_obj:
        #     try:
        #         await member_obj.edit(nick=remove_bot_suffix(member_obj.nick) if member_obj.nick else member_obj.name)
        #     except Exception as e:
        #         logging.warning(f"Nie udało się przywrócić nicku: {e}")
        save_data()
        try:
            await message.remove_reaction(emoji, user)
        except Exception as e:
            logging.warning(f"Nie udało się usunąć reakcji: {e}")


# ---------------------------------------------
# EVENT: on_message – PRZEKAZYWANIE KOMEND
# ---------------------------------------------
@bot.event
async def on_message(message):
    if message.author.bot:
        return
    await bot.process_commands(message)


# ---------------------------------------------
# DODATKOWA FUNKCJA: Zapewnienie dedykowanej roli bota
# ---------------------------------------------
async def ensure_bot_role(guild: discord.Guild):
    """Funkcja tworząca dedykowaną rolę dla bota (jeśli nie istnieje),
       przenosząca ją na szczyt hierarchii oraz przypisująca botowi.
       Upewnia się, że rola nie jest tworzona wielokrotnie."""
    bot_member = guild.get_member(bot.user.id)
    role_name = "BotRole"
    bot_role = discord.utils.get(guild.roles, name=role_name)

    if bot_role is None:
        bot_role = await guild.create_role(
            name=role_name,
            hoist=False,
            reason="Tworzenie dedykowanej roli dla bota"
        )
        logging.info(f"Rola {role_name} została utworzona na serwerze {guild.name}")
    else:
        logging.info(f"Rola {role_name} już istnieje na serwerze {guild.name}")

    max_position = len(guild.roles) - 1
    if bot_role.position != max_position:
        await bot_role.edit(position=max_position, reason="Przeniesienie roli bota na szczyt hierarchii")

    if bot_role not in bot_member.roles:
        await bot_member.add_roles(bot_role, reason="Przypisanie dedykowanej roli do bota")


# ---------------------------------------------
# EVENT: on_ready – GŁÓWNA INICJALIZACJA
# ---------------------------------------------
@bot.event
async def on_ready():
    try:
        await bot.change_presence(status=discord.Status.invisible)
        logging.info(f"Bot {bot.user} jest teraz niewidoczny.")
        logging.info(f"Zalogowano jako {bot.user}")
        load_data()
        for guild in bot.guilds:
            try:
                await ensure_bot_role(guild)
                logging.info(f"Rola bota zaktualizowana dla serwera: {guild.name}")
            except Exception as e:
                logging.error(f"Błąd przy aktualizacji roli na serwerze {guild.name}: {e}")
        for guild in bot.guilds:
            settings = get_guild_settings(guild)
            channel = guild.get_channel(settings.get("dedicated_channel_id"))
            if channel:
                try:
                    if settings.get("status_message_id"):
                        try:
                            old_msg = await channel.fetch_message(settings["status_message_id"])
                            await old_msg.delete()
                        except discord.NotFound:
                            pass
                    if settings.get("live_leaderboard_message_id"):
                        try:
                            old_lb = await channel.fetch_message(settings["live_leaderboard_message_id"])
                            await old_lb.delete()
                        except discord.NotFound:
                            pass
                    if settings.get("bac_leaderboard_message_id"):
                        try:
                            old_bac = await channel.fetch_message(settings["bac_leaderboard_message_id"])
                            await old_bac.delete()
                        except discord.NotFound:
                            pass
                except discord.Forbidden:
                    logging.warning(f"Brak uprawnień do usunięcia starych wiadomości na {guild.name}")
                await init_status_message_helper(guild, channel)
                await init_leaderboard_helper(guild, channel)
                await init_bac_leaderboard_helper(guild, channel)
            else:
                logging.warning(f"Dedykowany kanał nie ustawiony dla {guild.name}")
        update_tasks.start()
        update_owner_status_task.start()
        # update_all_nicknames.start()
    except Exception as e:
        logging.error(f"Exception in on_ready: {e}")


# ---------------------------------------------
# EVENT: on_guild_join – DLA NOWYCH SERWERÓW
# ---------------------------------------------
@bot.event
async def on_guild_join(guild: discord.Guild):
    try:
        await ensure_bot_role(guild)
        logging.info(f"Rola bota ustawiona dla nowego serwera: {guild.name}")
        settings = get_guild_settings(guild)
        channel = guild.get_channel(settings.get("dedicated_channel_id"))
        if channel:
            await init_status_message_helper(guild, channel)
            await init_leaderboard_helper(guild, channel)
            await init_bac_leaderboard_helper(guild, channel)
        else:
            logging.warning(f"Dedykowany kanał nie ustawiony dla {guild.name}")
    except Exception as e:
        logging.error(f"Błąd przy konfiguracji nowego serwera {guild.name}: {e}")


# ---------------------------------------------
# NOWE ZADANIE: AKTUALIZACJA STATUSU WŁAŚCICIELA CO GODZINĘ
# ---------------------------------------------
@tasks.loop(hours=1)
async def update_owner_status_task():
    for guild in bot.guilds:
        owner = await get_member(guild, guild.owner_id)
        if owner:
            # await update_nickname(owner)
            logging.info(f"Aktualizacja statusu właściciela {owner.name} na {guild.name}")
    save_data()


# ---------------------------------------------
# START BOTA
# ---------------------------------------------
if __name__ == "__main__":
    load_dotenv()
    TOKEN = os.getenv("DISCORD_TOKEN")
    if TOKEN:
        bot.run(TOKEN)
    else:
        logging.error("Brak tokena Discord!")
