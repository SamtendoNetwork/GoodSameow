import io
import os
from datetime import datetime, timezone

import boto3
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("TOKEN")
GUILD_ID = os.getenv("GUILD_ID")
TICKET_CATEGORY_ID = os.getenv("TICKET_CATEGORY_ID")

STAFF_ROLE_ID = 1512466821285154917
LOG_CHANNEL_ID = 1466887266575319164

R2_ENDPOINT_URL = os.getenv("R2_ENDPOINT_URL")
R2_ACCESS_KEY_ID = os.getenv("R2_ACCESS_KEY_ID")
R2_SECRET_ACCESS_KEY = os.getenv("R2_SECRET_ACCESS_KEY")
R2_BUCKET_NAME = os.getenv("R2_BUCKET_NAME")
R2_PUBLIC_URL_BASE = os.getenv("R2_PUBLIC_URL_BASE")

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

s3 = boto3.client(
    "s3",
    endpoint_url=R2_ENDPOINT_URL,
    aws_access_key_id=R2_ACCESS_KEY_ID,
    aws_secret_access_key=R2_SECRET_ACCESS_KEY,
)

TICKET_TOPIC_PREFIX = "ticket-owner-id:"


def sanitize_channel_name(name: str) -> str:
    cleaned = "".join(c if c.isalnum() or c == "-" else "-" for c in name.lower())
    return cleaned.strip("-") or "user"


def sanitize_filename(name: str) -> str:
    cleaned = "".join(c if c.isalnum() or c in ("-", "_", ".") else "_" for c in name)
    return cleaned.strip("_") or "file"


def is_ticket_channel(channel: discord.abc.GuildChannel) -> bool:
    return isinstance(channel, discord.TextChannel) and bool(
        channel.topic and channel.topic.startswith(TICKET_TOPIC_PREFIX)
    )


def find_open_ticket(guild: discord.Guild, user_id: int) -> discord.TextChannel | None:
    marker = f"{TICKET_TOPIC_PREFIX}{user_id}"
    for channel in guild.text_channels:
        if channel.topic and channel.topic == marker:
            return channel
    return None


async def upload_attachment_to_cdn(channel: discord.TextChannel, attachment: discord.Attachment) -> str | None:
    try:
        data = await attachment.read()
    except (discord.HTTPException, discord.NotFound):
        return None

    attachment_key = (
        f"transcripts/{channel.id}/attachments/{attachment.id}-{sanitize_filename(attachment.filename)}"
    )

    def upload():
        s3.put_object(
            Bucket=R2_BUCKET_NAME,
            Key=attachment_key,
            Body=io.BytesIO(data),
            ContentType=attachment.content_type or "application/octet-stream",
        )

    try:
        await bot.loop.run_in_executor(None, upload)
    except Exception:
        return None

    return f"{R2_PUBLIC_URL_BASE.rstrip('/')}/tickets/{attachment_key}"


class TicketPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Create Ticket", style=discord.ButtonStyle.green, custom_id="ticket_panel:create")
    async def create_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        member = interaction.user

        existing = find_open_ticket(guild, member.id)
        if existing is not None:
            await interaction.response.send_message(
                f"You already have an open ticket: {existing.mention}", ephemeral=True
            )
            return

        staff_role = guild.get_role(STAFF_ROLE_ID)
        category = None
        if TICKET_CATEGORY_ID:
            category = guild.get_channel(int(TICKET_CATEGORY_ID))

        await interaction.response.defer(ephemeral=True)

        channel = await guild.create_text_channel(
            name=f"ticket-{sanitize_channel_name(member.name)}",
            category=category,
            topic=f"{TICKET_TOPIC_PREFIX}{member.id}",
            reason=f"Ticket created by {member} ({member.id})",
        )

        await channel.set_permissions(member, read_messages=True, send_messages=True, read_message_history=True, attach_files=True)
        await channel.set_permissions(staff_role, read_messages=True, send_messages=True, read_message_history=True, attach_files=True)
        await channel.set_permissions(guild.default_role, view_channel=False)

        welcome_embed = discord.Embed(
            title="Ticket Created",
            description=f"Welcome, {member.mention}! Please describe your issue and a member of staff will be with you shortly.",
            color=discord.Color.green(),
        )

        ping_content = member.mention
        if staff_role is not None:
            ping_content += f" {staff_role.mention}"

        await channel.send(content=ping_content, embed=welcome_embed)
        await interaction.followup.send(f"Your ticket has been created: {channel.mention}", ephemeral=True)


@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.command(name="send-embed", description="Send the ticket creation embed.")
async def send_embed(interaction: discord.Interaction):
    embed = discord.Embed(
        title="Support Tickets",
        description="Click the button below to open a support ticket.",
        color=discord.Color.blurple(),
    )
    await interaction.channel.send(embed=embed, view=TicketPanelView())
    await interaction.response.send_message("Ticket panel sent.", ephemeral=True)


@app_commands.guild_only()
@app_commands.checks.has_role(STAFF_ROLE_ID)
@app_commands.describe(member="The member to add to this ticket.")
@app_commands.command(name="add", description="Add a member to this ticket.")
async def add_member(interaction: discord.Interaction, member: discord.Member):
    channel = interaction.channel

    if not is_ticket_channel(channel):
        await interaction.response.send_message(
            "This command can only be used inside a ticket channel.", ephemeral=True
        )
        return

    await channel.set_permissions(
        member,
        view_channel=True,
        send_messages=True,
        read_message_history=True,
        attach_files=True,
        reason=f"Added to ticket by {interaction.user}",
    )
    await interaction.response.send_message(f"{member.mention} has been added to this ticket.")


@app_commands.guild_only()
@app_commands.checks.has_role(STAFF_ROLE_ID)
@app_commands.describe(member="The member to remove from this ticket.")
@app_commands.command(name="remove", description="Remove a member from this ticket.")
async def remove_member(interaction: discord.Interaction, member: discord.Member):
    channel = interaction.channel

    if not is_ticket_channel(channel):
        await interaction.response.send_message(
            "This command can only be used inside a ticket channel.", ephemeral=True
        )
        return

    owner_id = int(channel.topic.removeprefix(TICKET_TOPIC_PREFIX))
    if member.id == owner_id:
        await interaction.response.send_message(
            "You can't remove the ticket owner this way. Use /close to close the ticket instead.",
            ephemeral=True,
        )
        return

    await channel.set_permissions(member, overwrite=None, reason=f"Removed from ticket by {interaction.user}")
    await interaction.response.send_message(f"{member.mention} has been removed from this ticket.")


@app_commands.guild_only()
@app_commands.checks.has_role(STAFF_ROLE_ID)
@app_commands.command(name="close", description="Close this ticket and save a transcript.")
async def close_ticket(interaction: discord.Interaction):
    channel = interaction.channel

    if not is_ticket_channel(channel):
        await interaction.response.send_message(
            "This command can only be used inside a ticket channel.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)

    owner_id = int(channel.topic.removeprefix(TICKET_TOPIC_PREFIX))
    owner = interaction.guild.get_member(owner_id)
    if owner is None:
        try:
            owner = await bot.fetch_user(owner_id)
        except discord.NotFound:
            owner = None

    lines = [f"Transcript for #{channel.name} (closed by {interaction.user} at {datetime.now(timezone.utc).isoformat()})", ""]
    async for message in channel.history(limit=None, oldest_first=True):
        timestamp = message.created_at.strftime("%Y-%m-%d %H:%M:%S UTC")
        content = message.content or "[no text content]"
        lines.append(f"[{timestamp}] {message.author} ({message.author.id}): {content}")
        for attachment in message.attachments:
            attachment_url = await upload_attachment_to_cdn(channel, attachment)
            if attachment_url:
                lines.append(f"    Attachment: {attachment_url}")
            else:
                lines.append(f"    Attachment (failed to upload, original may expire): {attachment.filename}")
    transcript_text = "\n".join(lines)

    object_key = f"transcripts/{channel.name}-{channel.id}.txt"

    def upload():
        s3.put_object(
            Bucket=R2_BUCKET_NAME,
            Key=object_key,
            Body=io.BytesIO(transcript_text.encode("utf-8")),
            ContentType="text/plain; charset=utf-8",
        )

    await bot.loop.run_in_executor(None, upload)
    transcript_url = f"{R2_PUBLIC_URL_BASE.rstrip('/')}/tickets/{object_key}"

    if owner is not None:
        try:
            await owner.send("Thanks for contacting us!")
        except (discord.Forbidden, discord.HTTPException):
            pass

    log_channel = bot.get_channel(LOG_CHANNEL_ID)
    if log_channel is None:
        try:
            log_channel = await bot.fetch_channel(LOG_CHANNEL_ID)
        except discord.NotFound:
            log_channel = None

    if log_channel is not None:
        log_embed = discord.Embed(
            title="Ticket Closed",
            description=f"Ticket `#{channel.name}` closed by {interaction.user.mention}.",
            color=discord.Color.red(),
        )
        log_embed.add_field(name="Ticket Owner", value=f"{owner.mention if owner else owner_id}", inline=True)
        log_embed.add_field(name="Transcript", value=transcript_url, inline=False)
        await log_channel.send(embed=log_embed)

    await interaction.followup.send("Ticket closed. Deleting channel...", ephemeral=True)
    await channel.delete(reason=f"Ticket closed by {interaction.user}")


@app_commands.guild_only()
@app_commands.checks.has_role(STAFF_ROLE_ID)
@app_commands.describe(member="The member to create the ticket for.")
@app_commands.command(name="force-create", description="Force create a ticket for a member.")
async def force_create_ticket(interaction: discord.Interaction, member: discord.Member):
    guild = interaction.guild

    existing = find_open_ticket(guild, member.id)
    if existing is not None:
        await interaction.response.send_message(
            f"{member.mention} already has an open ticket: {existing.mention}",
            ephemeral=True,
        )
        return

    staff_role = guild.get_role(STAFF_ROLE_ID)

    category = None
    if TICKET_CATEGORY_ID:
        category = guild.get_channel(int(TICKET_CATEGORY_ID))

    await interaction.response.defer(ephemeral=True)

    channel = await guild.create_text_channel(
        name=f"ticket-{sanitize_channel_name(member.name)}",
        category=category,
        topic=f"{TICKET_TOPIC_PREFIX}{member.id}",
        reason=f"Ticket force-created by {interaction.user} for {member} ({member.id})",
    )

    await channel.set_permissions(
        member,
        read_messages=True,
        send_messages=True,
        read_message_history=True,
        attach_files=True,
    )

    if staff_role is not None:
        await channel.set_permissions(
            staff_role,
            read_messages=True,
            send_messages=True,
            read_message_history=True,
            attach_files=True,
        )

    await channel.set_permissions(
        guild.default_role,
        view_channel=False,
    )

    welcome_embed = discord.Embed(
        title="Ticket Created",
        description=f"Welcome, {member.mention}! Please describe your issue and a member of staff will be with you shortly.",
        color=discord.Color.green(),
    )

    ping_content = member.mention
    if staff_role is not None:
        ping_content += f" {staff_role.mention}"

    await channel.send(
        content=ping_content,
        embed=welcome_embed,
    )

    await interaction.followup.send(
        f"Ticket created for {member.mention}: {channel.mention}",
        ephemeral=True,
    )


bot.tree.add_command(send_embed)
bot.tree.add_command(add_member)
bot.tree.add_command(remove_member)
bot.tree.add_command(close_ticket)
bot.tree.add_command(force_create_ticket)


@bot.event
async def on_ready():
    bot.add_view(TicketPanelView())
    if GUILD_ID:
        guild_obj = discord.Object(id=int(GUILD_ID))
        bot.tree.copy_global_to(guild=guild_obj)
        await bot.tree.sync(guild=guild_obj)
    else:
        await bot.tree.sync()
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print("Bot is ready!")


bot.run(TOKEN)