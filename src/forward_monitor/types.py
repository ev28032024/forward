from __future__ import annotations

from typing import TypedDict


class DiscordUser(TypedDict, total=False):
    id: str
    username: str
    global_name: str


class DiscordMember(TypedDict, total=False):
    nick: str


class DiscordChannelMention(TypedDict, total=False):
    id: str
    name: str


class DiscordAttachment(TypedDict, total=False):
    url: str
    proxy_url: str
    filename: str
    content_type: str
    size: int


class DiscordEmbedProvider(TypedDict, total=False):
    url: str


class DiscordEmbedMedia(TypedDict, total=False):
    url: str
    proxy_url: str


class DiscordEmbedField(TypedDict, total=False):
    name: str
    value: str


class DiscordEmbedFooter(TypedDict, total=False):
    text: str


class DiscordEmbedAuthor(TypedDict, total=False):
    name: str


class DiscordEmbed(TypedDict, total=False):
    title: str
    description: str
    url: str
    footer: DiscordEmbedFooter
    author: DiscordEmbedAuthor
    fields: list[DiscordEmbedField]
    image: DiscordEmbedMedia
    thumbnail: DiscordEmbedMedia
    video: DiscordEmbedMedia
    provider: DiscordEmbedProvider


class DiscordMessage(TypedDict, total=False):
    id: str
    channel_id: int
    guild_id: int
    content: str
    author: DiscordUser
    member: DiscordMember
    attachments: list[DiscordAttachment]
    embeds: list[DiscordEmbed]
    mentions: list[DiscordUser]
    mention_channels: list[DiscordChannelMention]
