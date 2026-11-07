from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import FSInputFile, InputSticker, Message, Sticker
from dotenv import load_dotenv

from .converter import ConversionError, ConvertedSticker, convert_sticker_file, convert_tgs_to_webm
from .storage import SettingsStore


logger = logging.getLogger(__name__)
router = Router()


def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Не задана переменная окружения {name}")
    return value


def sticker_set_name(user_id: int, bot_username: str, sticker_format: str) -> str:
    suffix = sticker_format[0]
    return f"e{user_id}{suffix}_by_{bot_username}"

def sticker_set_link(name: str) -> str:
    return f"https://t.me/addemoji/{name}"


def pack_title_fallback(user_name: str | None) -> str:
    if user_name:
        return f"{user_name}'s emoji pack"
    return "My emoji pack"


def source_suffix(sticker: Sticker) -> str:
    if sticker.is_video:
        return ".webm"
    if sticker.is_animated:
        return ".tgs"
    return ".webp"


def emoji_for_sticker(sticker: Sticker) -> str:
    return sticker.emoji or "🙂"


async def download_sticker(bot: Bot, sticker: Sticker, destination: Path) -> None:
    file = await bot.get_file(sticker.file_id)
    if not file.file_path:
        raise RuntimeError("Telegram не вернул file_path для стикера")
    await bot.download_file(file.file_path, destination=destination)


async def upload_converted_sticker(bot: Bot, user_id: int, converted: ConvertedSticker) -> str:
    uploaded = await bot.upload_sticker_file(
        user_id=user_id,
        sticker=FSInputFile(converted.path, filename=converted.path.name),
        sticker_format=converted.sticker_format,
    )
    return uploaded.file_id


async def ensure_emoji_set(
    bot: Bot,
    user_id: int,
    bot_username: str,
    first_sticker: ConvertedSticker,
    emoji: str,
    title: str,
) -> str:
    name = sticker_set_name(user_id, bot_username, first_sticker.sticker_format)
    try:
        await bot.get_sticker_set(name)
        return name
    except TelegramBadRequest as exc:
        if "STICKERSET_INVALID" not in exc.message and "sticker set not found" not in exc.message.lower():
            raise

    uploaded_file_id = await upload_converted_sticker(bot, user_id, first_sticker)
    await bot.create_new_sticker_set(
        user_id=user_id,
        name=name,
        title=title,
        sticker_format=first_sticker.sticker_format,
        stickers=[
            InputSticker(
                sticker=uploaded_file_id,
                emoji_list=[emoji],
                format=first_sticker.sticker_format,
            )
        ],
        sticker_type="custom_emoji",
    )
    return name


async def add_or_create_emoji(
    bot: Bot,
    user_id: int,
    bot_username: str,
    converted: ConvertedSticker,
    emoji: str,
    title: str,
) -> str:
    name = sticker_set_name(user_id, bot_username, converted.sticker_format)
    try:
        await bot.get_sticker_set(name)
    except TelegramBadRequest as exc:
        if "STICKERSET_INVALID" in exc.message or "sticker set not found" in exc.message.lower():
            return await ensure_emoji_set(bot, user_id, bot_username, converted, emoji, title)
        raise

    uploaded_file_id = await upload_converted_sticker(bot, user_id, converted)
    await bot.add_sticker_to_set(
        user_id=user_id,
        name=name,
        sticker=InputSticker(
            sticker=uploaded_file_id,
            emoji_list=[emoji],
            format=converted.sticker_format,
        ),
    )
    return name


async def safe_edit_status(message: Message | None, text: str) -> None:
    if message is None:
        return
    try:
        await message.edit_text(text)
    except Exception:
        logger.debug("Не удалось обновить статус", exc_info=True)


def parse_sticker_set_name(raw_link: str) -> str | None:
    value = raw_link.strip()
    if not value:
        return None

    if "/" not in value and "?" not in value:
        return value

    parsed = urlparse(value)
    path_parts = [part for part in parsed.path.split("/") if part]
    if path_parts and path_parts[-2:-1] in (["addstickers"], ["addemoji"]):
        return path_parts[-1]

    query = parse_qs(parsed.query)
    if "set" in query and query["set"]:
        return query["set"][0]

    return None


def describe_telegram_error(exc: TelegramBadRequest) -> str:
    message = exc.message
    if "STICKERS_TOO_MUCH" in message or "too many stickers" in message.lower():
        return "Пак переполнен. Создайте новый пак или удалите часть эмодзи."
    if "STICKER_PNG_DIMENSIONS" in message or "STICKER_WEBM_DIMENSIONS" in message:
        return "Telegram отклонил размер файла. Конвертация не прошла в строгий формат 100x100."
    if "STICKER_FILE_SIZE" in message or "file is too big" in message.lower():
        return "Telegram отклонил файл по весу. Нужно сильнее сжать исходник."
    if "USER_IS_NOT_PREMIUM" in message:
        return "Для создания custom emoji пользователь должен иметь Telegram Premium."
    return f"Telegram отклонил файл: {message}"


def is_wrong_file_type_error(exc: TelegramBadRequest) -> bool:
    return "wrong file type" in exc.message.lower()


async def convert_one_sticker(
    bot: Bot,
    sticker: Sticker,
    user_id: int,
    bot_username: str,
    workdir: Path,
    title: str,
    status: Message | None = None,
) -> str:
    source = workdir / f"{sticker.file_unique_id}{source_suffix(sticker)}"
    await download_sticker(bot, sticker, source)
    await safe_edit_status(status, "Стикер скачан. Готовлю файл для emoji pack...")

    converted = await convert_sticker_file(
        source,
        workdir,
        is_video=sticker.is_video,
        is_animated=sticker.is_animated,
    )
    await safe_edit_status(status, "Файл подготовлен. Загружаю в Telegram...")
    try:
        pack_name = await add_or_create_emoji(
            bot=bot,
            user_id=user_id,
            bot_username=bot_username,
            converted=converted,
            emoji=emoji_for_sticker(sticker),
            title=title,
        )
    except TelegramBadRequest as exc:
        if not sticker.is_animated or not is_wrong_file_type_error(exc):
            raise

        await safe_edit_status(
            status,
            "Telegram отклонил TGS. Рендерю в WEBM, это может занять до пары минут...",
        )
        converted = await convert_tgs_to_webm(source, workdir)
        await safe_edit_status(status, "WEBM готов. Повторно загружаю в Telegram...")
        pack_name = await add_or_create_emoji(
            bot=bot,
            user_id=user_id,
            bot_username=bot_username,
            converted=converted,
            emoji=emoji_for_sticker(sticker),
            title=title,
        )
    return pack_name


@router.message(Command("start", "help"))
async def help_handler(message: Message) -> None:
    await message.answer(
        "Отправьте обычный стикер, и я добавлю его как custom emoji в ваш персональный пак.\n"
        "Также можно импортировать пак командой: /convert_pack <ссылка на t.me/addstickers/...>\n"
        "Имя пака меняется командой: /set_pack_title <название>"
    )


@router.message(Command("set_pack_title"))
async def set_pack_title_handler(message: Message, command: CommandObject, settings: SettingsStore) -> None:
    if not message.from_user:
        return

    title = (command.args or "").strip()
    if not title:
        await message.answer("Укажите название пака: /set_pack_title Мой эмодзи пак")
        return

    await settings.set_pack_title(message.from_user.id, title)
    await message.answer(f"Сохранено название пака: {title}")


@router.message(Command("convert_pack"))
async def convert_pack_handler(
    message: Message,
    command: CommandObject,
    bot: Bot,
    bot_username: str,
    settings: SettingsStore,
) -> None:
    if not message.from_user:
        return

    set_name = parse_sticker_set_name(command.args or "")
    if not set_name:
        await message.answer("Укажите ссылку или имя пака: /convert_pack https://t.me/addstickers/name")
        return

    status = await message.answer("Загружаю пак и начинаю конвертацию...")

    title = await settings.get_pack_title(message.from_user.id, pack_title_fallback(message.from_user.first_name))

    try:
        source_set = await bot.get_sticker_set(set_name)
    except TelegramBadRequest as exc:
        await status.edit_text(describe_telegram_error(exc))
        return

    added = 0
    failed = 0
    pack_name = sticker_set_name(message.from_user.id, bot_username, "animated")

    with tempfile.TemporaryDirectory(prefix="emoji_pack_") as tmp:
        workdir = Path(tmp)
        for sticker in source_set.stickers:
            try:
                pack_name = await convert_one_sticker(bot, sticker, message.from_user.id, bot_username, workdir, title)
                added += 1
            except (ConversionError, TelegramBadRequest) as exc:
                failed += 1
                logger.warning("Не удалось сконвертировать стикер %s: %s", sticker.file_unique_id, exc)
            except Exception:
                failed += 1
                logger.exception("Неожиданная ошибка при конвертации стикера %s", sticker.file_unique_id)

    await status.edit_text(
        f"Готово. Добавлено: {added}, ошибок: {failed}\n"
        f"Пак: {sticker_set_link(pack_name)}"
    )


@router.message(F.sticker)
async def sticker_handler(message: Message, bot: Bot, bot_username: str, settings: SettingsStore) -> None:
    if not message.sticker or not message.from_user:
        return

    status = await message.answer("Конвертирую стикер в custom emoji...")
    title = await settings.get_pack_title(message.from_user.id, pack_title_fallback(message.from_user.first_name))

    try:
        with tempfile.TemporaryDirectory(prefix="emoji_one_") as tmp:
            pack_name = await convert_one_sticker(
                bot=bot,
                sticker=message.sticker,
                user_id=message.from_user.id,
                bot_username=bot_username,
                workdir=Path(tmp),
                title=title,
                status=status,
            )
    except ConversionError as exc:
        await status.edit_text(f"Не удалось сконвертировать файл: {exc}")
        return
    except TelegramBadRequest as exc:
        await status.edit_text(describe_telegram_error(exc))
        return
    except Exception:
        logger.exception("Неожиданная ошибка")
        await status.edit_text("Произошла внутренняя ошибка при обработке стикера.")
        return

    await status.edit_text(f"Эмодзи добавлен в пак:\n{sticker_set_link(pack_name)}")


async def main() -> None:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    bot = Bot(token=require_env("BOT_TOKEN"))
    database_url = os.getenv("DATABASE_URL") or os.getenv("POSTGRES_DSN")
    if not database_url:
        raise RuntimeError("Не задана переменная окружения DATABASE_URL или POSTGRES_DSN")

    settings = SettingsStore(database_url)
    await settings.init()

    me = await bot.get_me()
    if not me.username:
        raise RuntimeError("У бота должен быть username")

    dp = Dispatcher(bot_username=me.username, settings=settings)
    dp.include_router(router)

    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
