import re
import asyncio
import requests
from datetime import timedelta
import discord
from discord import app_commands
from discord.ext import commands
import os
import dotenv
from dotenv import laod_dotenv

load_dotenv()


# ---------- Configuration ----------
GUILD_ID = 1508632632152555611  # <-- put your guild id here as an integer
GOOGLE_DOC_ID = "1Uuq9YirOfadx3a07n6_0Ev1F7Gz4Yf-t6FnNU87BHqQ"
GOOGLE_DOC_TXT_URL = f"https://docs.google.com/document/d/{GOOGLE_DOC_ID}/export?format=txt"
Audit_log_channel = 1508632634945966253  # log channel ID
MUTED_ROLE_ID = 1508632632186114064      # existing Muted role ID
T_MODS_ROLE_ID = 1508632632152555616     # t mods role ID

# DM templates
DM_BAN = "@{user}\nYou have Been Banned from PGC"
DM_TEMP_BAN = "@{user}\nYour Have Been temp banned from PGC here is how you can apply to get yourself unbanned [PGC Temp Ban Form](https://docs.google.com/forms/d/e/1FAIpQLScWBwnnXTV3528tG-L4bW93zfwMqSev7vD-kwH0c6eP0-zLpQ/viewform?usp=header)"
DM_KICK = "@{user}\nYou Have Been Kicked from PGC and you can rejoin"
DM_TIMEOUT = "@{user}\nYou have been timeout from PGC here is how you can get unmuted [PGC UnMuted Form](https://docs.google.com/forms/d/e/1FAIpQLSeksfFyZ-PSpp4AADrheYyebpKfiGNmGIwfsV2A-5UQzOzepw/viewform?usp=header)"

# Discord timeout max (28 days)
DISCORD_TIMEOUT_MAX_SECONDS = 28 * 24 * 3600

intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# In-memory scheduled tasks (runtime only)
scheduled_unbans = {}
scheduled_untimeouts = {}
scheduled_unmutes = {}

# ---------- Helpers ----------
def fetch_doc_text():
    try:
        r = requests.get(GOOGLE_DOC_TXT_URL, timeout=10)
        r.raise_for_status()
        return r.text
    except Exception:
        return ""

def determine_punishment(offense_text):
    doc_text = fetch_doc_text().lower()
    lookup = {
        "ban": ["permanent ban", "ban", "banned", "permanently banned"],
        "tempban": ["temp ban", "temporary ban", "tempban", "temporary banned"],
        "kick": ["kick", "kicked"],
        "timeout": ["timeout", "mute", "timed out", "timeouted"],
    }
    for action, keywords in lookup.items():
        for kw in keywords:
            if kw in doc_text:
                return action
    ot = (offense_text or "").lower()
    for action, keywords in lookup.items():
        for kw in keywords:
            if kw in ot:
                return action
    return None

def parse_duration_to_seconds(s: str) -> int:
    if not s:
        return 0
    s = s.strip().lower()
    total = 0
    patterns = {'d': 86400, 'h': 3600, 'm': 60, 's': 1}
    for match in re.finditer(r'(\d+)\s*([dhms])', s):
        val = int(match.group(1))
        unit = match.group(2)
        total += val * patterns.get(unit, 0)
    if total == 0:
        try:
            total = int(s)
        except:
            total = 0
    return max(total, 0)

async def get_muted_role_by_id(guild: discord.Guild) -> discord.Role:
    role = guild.get_role(MUTED_ROLE_ID)
    if role:
        return role
    return discord.utils.get(guild.roles, name="Muted")

def member_has_role(member: discord.Member, role_id: int) -> bool:
    return any(r.id == role_id for r in member.roles)

def is_admin(member: discord.Member) -> bool:
    return member.guild_permissions.administrator

# ---------- Logging ----------
async def log_command(interaction: discord.Interaction) -> bool:
    if interaction.command is None:
        return True
    if interaction.type != discord.InteractionType.application_command:
        return True
    channel = interaction.guild.get_channel(Audit_log_channel)
    if channel:
        logging = interaction.data.get("options", [])
        parts = []
        for option in logging:
            if option.get("value") is not None:
                value = f"<@{option['value']}>" if option["name"] in ("user", "player") else option["value"]
                parts.append(f"{option['name']}: `{value}`")
            elif option.get("options"):
                for sub in option["options"]:
                    val = f"<@{sub['value']}>" if sub["name"] in ("user", "player") else sub.get("value")
                    parts.append(f"{sub['name']}: `{val}`")
        embed = discord.Embed(description=f"**/{interaction.command.name}**" + (f"\n{' '.join(parts)}" if parts else ""))
        embed.set_author(name=interaction.user.name, icon_url=interaction.user.display_avatar.url)
        embed.timestamp = discord.utils.utcnow()
        try:
            await channel.send(embed=embed)
        except Exception:
            pass
    return True

# ---------- Scheduled tasks (use aware datetimes) ----------
async def schedule_unban(guild: discord.Guild, user_id: int, when: discord.datetime):
    now = discord.utils.utcnow()
    delay = (when - now).total_seconds()
    if delay <= 0:
        try:
            await guild.unban(discord.Object(id=user_id), reason="Tempban expired")
        except Exception:
            pass
        return
    scheduled_unbans[user_id] = when
    await asyncio.sleep(delay)
    try:
        await guild.unban(discord.Object(id=user_id), reason="Tempban expired")
    except Exception:
        pass
    scheduled_unbans.pop(user_id, None)

async def schedule_remove_timeout(guild: discord.Guild, user_id: int, when: discord.datetime, via_role=False):
    now = discord.utils.utcnow()
    delay = (when - now).total_seconds()
    if delay <= 0:
        if via_role:
            role = await get_muted_role_by_id(guild)
            member = guild.get_member(user_id)
            if member and role:
                try:
                    await member.remove_roles(role, reason="Mute expired")
                except Exception:
                    pass
        else:
            member = guild.get_member(user_id)
            try:
                if member:
                    await member.edit(timed_out_until=None, reason="Timeout expired")
            except Exception:
                pass
        return
    if via_role:
        scheduled_unmutes[user_id] = when
    else:
        scheduled_untimeouts[user_id] = when
    await asyncio.sleep(delay)
    if via_role:
        role = await get_muted_role_by_id(guild)
        member = guild.get_member(user_id)
        if member and role:
            try:
                await member.remove_roles(role, reason="Mute expired")
            except Exception:
                pass
        scheduled_unmutes.pop(user_id, None)
    else:
        member = guild.get_member(user_id)
        try:
            if member:
                await member.edit(timed_out_until=None, reason="Timeout expired")
        except Exception:
            pass
        scheduled_untimeouts.pop(user_id, None)

# ---------- Commands ----------
@tree.command(name="punishment", description="Apply a punishment to a user", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(user="Member to punish", action="ban,kick,timeout,tempban OR 'auto' to consult doc", reason="Reason or offense text", duration="Duration for tempban/timeout (e.g. 2d,12h,30m)")
async def punishment(interaction: discord.Interaction, user: discord.Member, action: str, reason: str, duration: str = None):
    await interaction.response.defer(ephemeral=False)
    await log_command(interaction)

    action = action.lower()
    if action == "auto":
        resolved = determine_punishment(reason)
        if not resolved:
            await interaction.followup.send("Could not determine punishment from document or reason. Please specify action explicitly.", ephemeral=True)
            return
        action = resolved

    author = interaction.user
    # Permission enforcement
    if action in ("ban", "tempban", "temp-ban", "temp_ban"):
        if not is_admin(author):
            await interaction.followup.send("You must have the Administrator permission to perform this action.", ephemeral=True)
            return
    if action in ("kick", "timeout", "mute"):
        if not (member_has_role(author, T_MODS_ROLE_ID) or is_admin(author)):
            await interaction.followup.send("You must have the T Mods role (or Administrator) to perform this action.", ephemeral=True)
            return

    try:
        if action == "ban":
            try:
                await user.send(DM_BAN.format(user=user.name))
            except Exception:
                pass
            await interaction.guild.ban(user, reason=reason)
            await interaction.followup.send(f"{user.mention} has been banned. Reason: {reason}")

        elif action == "kick":
            try:
                await user.send(DM_KICK.format(user=user.name))
            except Exception:
                pass
            await user.kick(reason=reason)
            await interaction.followup.send(f"{user.mention} has been kicked. Reason: {reason}")

        elif action in ("timeout", "mute"):
            seconds = parse_duration_to_seconds(duration) if duration else 3600
            until = discord.utils.utcnow() + timedelta(seconds=seconds)
            if seconds <= DISCORD_TIMEOUT_MAX_SECONDS:
                try:
                    await user.send(DM_TIMEOUT.format(user=user.name))
                except Exception:
                    pass
                await user.edit(timed_out_until=until, reason=reason)
                bot.loop.create_task(schedule_remove_timeout(interaction.guild, user.id, until, via_role=False))
                await interaction.followup.send(f"{user.mention} has been timed out until {until.isoformat()} UTC. Reason: {reason}")
            else:
                role = await get_muted_role_by_id(interaction.guild)
                if role is None:
                    await interaction.followup.send("Muted role not found; cannot apply long timeout.", ephemeral=True)
                    return
                try:
                    await user.send(DM_TIMEOUT.format(user=user.name))
                except Exception:
                    pass
                await user.add_roles(role, reason=f"Long timeout: {reason}")
                bot.loop.create_task(schedule_remove_timeout(interaction.guild, user.id, until, via_role=True))
                await interaction.followup.send(f"{user.mention} has been muted with role '{role.name}' until {until.isoformat()} UTC. Reason: {reason}")

        elif action in ("tempban", "temp-ban", "temp_ban"):
            if not duration:
                await interaction.followup.send("You must provide a duration for temp ban (e.g. '2d', '12h').", ephemeral=True)
                return
            seconds = parse_duration_to_seconds(duration)
            if seconds <= 0:
                await interaction.followup.send("Invalid duration format.", ephemeral=True)
                return
            unban_at = discord.utils.utcnow() + timedelta(seconds=seconds)
            try:
                await user.send(DM_TEMP_BAN.format(user=user.name))
            except Exception:
                pass
            await interaction.guild.ban(user, reason=reason)
            bot.loop.create_task(schedule_unban(interaction.guild, user.id, unban_at))
            await interaction.followup.send(f"{user.mention} has been temp-banned until {unban_at.isoformat()} UTC. Reason: {reason}")

        else:
            await interaction.followup.send("Unknown action. Use ban, kick, timeout, tempban, or auto.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"Failed to perform action: {e}", ephemeral=True)

@tree.command(name="unban", description="Unban a user by ID", guild=discord.Object(id=GUILD_ID))
@app_commands.describe(user_id="User ID to unban", reason="Reason for unban")
async def unban_cmd(interaction: discord.Interaction, user_id: str, reason: str = None):
    await interaction.response.defer(ephemeral=False)
    await log_command(interaction)
    # Admins (Discord Administrator permission) only
    if not is_admin(interaction.user):
        await interaction.followup.send("You must have the Administrator permission to unban.", ephemeral=True)
        return
    try:
        uid = int(user_id)
        await interaction.guild.unban(discord.Object(id=uid), reason=reason)
        scheduled_unbans.pop(uid, None)
        await interaction.followup.send(f"Unbanned user ID {uid}. Reason: {reason or 'No reason provided'}")
    except Exception as e:
        await interaction.followup.send(f"Failed to unban: {e}", ephemeral=True)

# ---------- Bot startup ----------
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    try:
        await tree.sync(guild=discord.Object(id=GUILD_ID))
    except Exception:
        await tree.sync()
    print("Commands synced.")

if __name__ == "__main__":
    bot.run(os.getenv("BOT_TOKEN"))
