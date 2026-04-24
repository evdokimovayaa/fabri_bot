from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Optional

from obabot import create_bot
from obabot.context import get_current_platform
from obabot.filters import CommandStart, F
from obabot.fsm import FSMContext, MemoryStorage, State, StatesGroup
from obabot.types import (
    BPlatform,
    BufferedInputFile,
    InlineKeyboardButton,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

# Import all business logic from core.py
from core import (
    CONSENT_DECLINE_PHONE,
    HOTLINE_PHONE,
    HOTLINE_REMINDERS,
    STEPS,
    _is_doctor,
    _normalize_spaces,
    _utc_iso,
    _wants_callback,
    _wants_sms,
    _should_recommend_doctor_followup,
    build_group_report,
    build_survey_result,
    calculate_fabry_score_details,
    generate_pdf_report,
    get_score_interpretation,
    next_step_index,
    step_by_index,
)


# =========================
# Configuration
# =========================

load_dotenv()

TEST_MODE = os.getenv("TEST_MODE", "0").strip().lower() in {"1", "true", "yes", "on"}
TEST_BOT_TOKEN = os.getenv("TEST_BOT_TOKEN", "").strip()
TEST_GROUP_CHAT_ID_RAW = os.getenv("TEST_GROUP_CHAT_ID", "").strip()

# Telegram
BOT_TOKEN = TEST_BOT_TOKEN if TEST_MODE else os.getenv("BOT_TOKEN", "").strip()
GROUP_CHAT_ID_RAW = (
    TEST_GROUP_CHAT_ID_RAW if TEST_MODE else os.getenv("GROUP_CHAT_ID", "").strip()
)
GROUP_CHAT_ID: Optional[int] = int(GROUP_CHAT_ID_RAW) if GROUP_CHAT_ID_RAW else None
LOG_CHAT_ID_RAW = (
    TEST_GROUP_CHAT_ID_RAW if TEST_MODE else os.getenv("LOG_CHAT_ID", "").strip()
)
LOG_CHAT_ID: Optional[int] = int(LOG_CHAT_ID_RAW) if LOG_CHAT_ID_RAW else GROUP_CHAT_ID

# Max
MAX_BOT_TOKEN = os.getenv("MAX_BOT_TOKEN", "").strip()
MAX_GROUP_CHAT_ID_RAW = os.getenv("MAX_GROUP_CHAT_ID", "").strip()
MAX_GROUP_CHAT_ID: Optional[int] = (
    int(MAX_GROUP_CHAT_ID_RAW) if MAX_GROUP_CHAT_ID_RAW else None
)


# =========================
# Logging
# =========================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("medical_intake_bot")
logging.getLogger("aiogram.event").setLevel(logging.WARNING)


class TelegramLogHandler(logging.Handler):
    """Forward application logs to Telegram group asynchronously."""

    def __init__(self, tg_bot, log_chat_id: int):
        super().__init__()
        self.tg_bot = tg_bot
        self.log_chat_id = log_chat_id

    def emit(self, record: logging.LogRecord) -> None:
        if not self.tg_bot or not self.log_chat_id:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        message = self.format(record)
        loop.create_task(self._send_log(message))

    async def _send_log(self, text: str) -> None:
        if not self.tg_bot or not self.log_chat_id:
            return
        try:
            # Telegram allows up to 4096 chars per message.
            await self.tg_bot.send_message(self.log_chat_id, text[:4000])
        except Exception:
            # Do not recursively log handler errors.
            pass


# =========================
# Global state
# =========================

bot = None
dp = None
router = None
admin_forwarding_enabled = True
_pdf_data_cache: dict[int, dict[str, Any]] = {}


class SurveyFSM(StatesGroup):
    waiting_consent = State()
    waiting_choice = State()
    waiting_text = State()
    collecting_additional = State()


# =========================
# Validators (moved from main.py, not in core.py)
# =========================

def validate_age(text: str, _: dict[str, Any]) -> tuple[bool, str]:
    text = text.strip()
    if not text.isdigit():
        return False, "Пожалуйста, введите возраст только цифрами (например: 35)."
    age = int(text)
    if age < 0 or age > 120:
        return False, "Пожалуйста, проверьте возраст (допустимый диапазон: 0–120)."
    return True, ""


def validate_nonempty(text: str, _: dict[str, Any]) -> tuple[bool, str]:
    if not _normalize_spaces(text):
        return False, "Пожалуйста, введите ответ текстом."
    return True, ""


def validate_full_name(text: str, _: dict[str, Any]) -> tuple[bool, str]:
    import re
    t = _normalize_spaces(text)
    if len(t) < 2:
        return False, "Пожалуйста, укажите ваше ФИО."
    if not re.match(r"^[А-Яа-яЁёA-Za-z\s.\-]+$", t):
        return False, "ФИО может содержать только буквы, пробелы, дефисы и точки."
    return True, ""


def validate_phone(text: str, _: dict[str, Any]) -> tuple[bool, str]:
    import re
    t = _normalize_spaces(text)
    if not re.match(r"^[\d\s+\-()\,]+$", t):
        return False, "Номер телефона содержит недопустимые символы. Используйте только цифры, +, -, (, )."
    digits = re.sub(r"\D", "", t)
    if len(digits) < 10:
        return False, "Не вижу номера телефона. Можно в формате +7XXXXXXXXXX или 8XXXXXXXXXX."
    if len(digits) > 15:
        return False, "Слишком длинный номер. Проверьте, пожалуйста."
    return True, ""


# =========================
# Keyboards
# =========================

def hotline_keyboard_row(builder: InlineKeyboardBuilder) -> None:
    builder.row(
        InlineKeyboardButton(
            text="📞 Позвонить на горячую линию",
            callback_data="hotline",
        )
    )


def consent_keyboard() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Я согласен на обработку данных", callback_data="consent|yes")
    kb.button(text="❌ Я не согласен", callback_data="consent|no")
    kb.adjust(1)
    hotline_keyboard_row(kb)
    return kb


def choice_keyboard(step_index: int, options: list[str]) -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    for i, opt in enumerate(options):
        kb.button(text=opt, callback_data=f"ans|{step_index}|{i}")
    kb.adjust(1)
    hotline_keyboard_row(kb)
    return kb


def text_keyboard() -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    hotline_keyboard_row(kb)
    return kb


def phone_reply_keyboard() -> ReplyKeyboardMarkup:
    """Reply keyboard with 'Share contact' button for the phone step."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Поделиться номером", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def collect_keyboard(step_index: int) -> InlineKeyboardBuilder:
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Продолжить", callback_data=f"collect_done|{step_index}")
    kb.adjust(1)
    hotline_keyboard_row(kb)
    return kb


# =========================
# Helpers
# =========================

async def get_data(state: FSMContext) -> dict[str, Any]:
    return await state.get_data()


async def _track_msg(state: FSMContext, *msg_ids: int) -> None:
    """Add message IDs to the deletable list."""
    data = await state.get_data()
    ids = list(data.get("_del_ids", []))
    ids.extend(msg_ids)
    await state.update_data(_del_ids=ids)


async def _delete_tracked(chat_id: int, state: FSMContext) -> None:
    """Delete all tracked messages and clear the list."""
    data = await state.get_data()
    ids = data.get("_del_ids", [])
    for mid in ids:
        try:
            await bot.delete_message(chat_id, mid)
        except Exception:
            logger.warning("Failed to delete message %s in chat %s", mid, chat_id, exc_info=True)
    if ids:
        await state.update_data(_del_ids=[])


async def _send_to_group(text: str) -> None:
    """Send message to group chat on the current platform."""
    global admin_forwarding_enabled

    if not admin_forwarding_enabled:
        return

    platform = get_current_platform()

    if platform == BPlatform.telegram and GROUP_CHAT_ID:
        try:
            await bot.send_message(GROUP_CHAT_ID, text, platform=BPlatform.telegram)
        except TelegramBadRequest as e:
            if "chat not found" in str(e).lower():
                admin_forwarding_enabled = False
                logger.warning(
                    "Group forwarding disabled: chat not found for GROUP_CHAT_ID=%s. "
                    "Проверьте ID группы и убедитесь, что бот добавлен в группу.",
                    GROUP_CHAT_ID,
                )
            else:
                logger.exception("Failed to send data to Telegram group chat")
        except Exception:
            logger.exception("Failed to send data to Telegram group chat")

    elif platform == BPlatform.max and MAX_GROUP_CHAT_ID:
        try:
            await bot.send_message(MAX_GROUP_CHAT_ID, text, platform=BPlatform.max)
        except Exception:
            logger.exception("Failed to send data to Max group chat")


async def _send_attachments_to_group(additional_payload: list[dict[str, Any]]) -> None:
    """Send all collected attachments to group chat on the current platform."""
    if not additional_payload or not admin_forwarding_enabled:
        return

    platform = get_current_platform()
    group_chat_id = GROUP_CHAT_ID if platform == BPlatform.telegram else MAX_GROUP_CHAT_ID

    if not group_chat_id:
        return

    for item in additional_payload:
        try:
            item_type = item.get("type", "other")

            if item_type == "text":
                # Send text as caption in a message
                text = item.get("text", "")
                if text:
                    await bot.send_message(group_chat_id, f"📝 {text}", platform=platform)

            elif item_type == "photo":
                file_id = item.get("file_id")
                if file_id:
                    await bot.send_photo(group_chat_id, file_id, platform=platform)

            elif item_type == "document":
                file_id = item.get("file_id")
                file_name = item.get("file_name", "документ")
                if file_id:
                    await bot.send_document(
                        group_chat_id, file_id, caption=f"📄 {file_name}", platform=platform
                    )

            elif item_type == "voice":
                file_id = item.get("file_id")
                if file_id:
                    await bot.send_voice(group_chat_id, file_id, platform=platform)

            elif item_type == "audio":
                file_id = item.get("file_id")
                file_name = item.get("file_name", "аудио")
                if file_id:
                    await bot.send_audio(
                        group_chat_id, file_id, title=file_name, platform=platform
                    )

            elif item_type == "video":
                file_id = item.get("file_id")
                if file_id:
                    await bot.send_video(group_chat_id, file_id, platform=platform)

        except Exception:
            logger.exception(
                "Failed to send attachment (type=%s) to group chat", item.get("type")
            )


async def send_step(message, state: FSMContext) -> None:
    data = await state.get_data()
    idx = data.get("step_index", 0)

    # Ensure idx points to a valid conditional step.
    valid_idx = next_step_index(idx, data)
    if valid_idx is None:
        await finish_survey(message, state)
        return

    if valid_idx != idx:
        idx = valid_idx
        await state.update_data(step_index=idx)

    # Delete previous question/answer messages
    await _delete_tracked(message.chat.id, state)

    track_ids: list[int] = []

    if idx in HOTLINE_REMINDERS:
        reminder = await message.answer(HOTLINE_REMINDERS[idx])
        track_ids.append(reminder.message_id)

    step = step_by_index(idx)
    text = step.text(data)

    if step.kind == "choice":
        opts = step.options(data) if step.options else []
        markup = choice_keyboard(idx, opts).as_markup()
        await state.set_state(SurveyFSM.waiting_choice)
    elif step.kind == "text":
        markup = text_keyboard().as_markup()
        await state.set_state(SurveyFSM.waiting_text)
    else:
        markup = collect_keyboard(idx).as_markup()
        await state.set_state(SurveyFSM.collecting_additional)

    sent = await message.answer(text, reply_markup=markup)
    track_ids.append(sent.message_id)

    # For the phone step, show reply keyboard only on Telegram (Max doesn't support it)
    if step.key == "phone" and not message.is_max():
        share_msg = await message.answer(
            "Или нажмите кнопку ниже, чтобы поделиться номером:",
            reply_markup=phone_reply_keyboard(),
        )
        track_ids.append(share_msg.message_id)

    await _track_msg(state, *track_ids)


async def finish_survey(message, state: FSMContext) -> None:
    global admin_forwarding_enabled

    data = await state.get_data()
    user_id = message.from_user.id
    chat_id = message.chat.id
    username = message.from_user.username

    # Delete previous question messages
    await _delete_tracked(chat_id, state)

    wants_callback = _wants_callback(data)

    if wants_callback:
        await message.answer(
            "Спасибо за ответы! Ваши данные переданы специалисту. Ожидайте звонка в ближайшее время."
        )
    else:
        await message.answer("Спасибо за ответы! Ваши данные переданы специалисту.")

    answers = data.get("answers", {})
    fabry_score, score_breakdown = calculate_fabry_score_details(answers)
    score_interpretation = get_score_interpretation(fabry_score)

    is_doctor = _is_doctor(data)
    doctor_followup_required = is_doctor and _should_recommend_doctor_followup(answers)
    if doctor_followup_required:
        data["doctor_followup_reason"] = "family_history_fabry"

    diagnostics_needed = doctor_followup_required or fabry_score >= 3

    if not is_doctor:
        if diagnostics_needed and wants_callback:
            info = (
                "На основе ваших ответов выявлено сходство некоторых признаков с Болезнью Фабри. "
                "Болезнь Фабри – это редкое генетическое заболевание.\n\n"
                "Рекомендуем вам распечатать результаты этого диалога и записаться на прием к врачу-неврологу или генетику.\n\n"
                "Для точной диагностики необходимо сдать генетический анализ и провести анализ уровня фермента альфа-галактозидазы.\n\n"
                f"Вы также можете позвонить по телефону горячей линии: {HOTLINE_PHONE}."
            )
        elif diagnostics_needed:
            info = (
                "На основе ваших ответов выявлено сходство некоторых признаков с Болезнью Фабри. "
                "Болезнь Фабри – это редкое генетическое заболевание.\n\n"
                "Рекомендуем вам распечатать результаты этого диалога и записаться на прием к врачу-неврологу или генетику.\n\n"
                "Для точной диагностики необходимо сдать генетический анализ и провести анализ уровня фермента альфа-галактозидазы."
            )
        elif wants_callback:
            info = (
                "По результатам анкеты выраженных признаков болезни Фабри не выявлено.\n\n"
                "Если хотите уточнить результаты, специалист может связаться с вами по телефону горячей линии."
            )
        else:
            info = (
                "По результатам анкеты выраженных признаков болезни Фабри не выявлено.\n\n"
                "При сохранении жалоб обратитесь к врачу для очной консультации."
            )
    else:
        if doctor_followup_required:
            info = (
                "У пациента есть кровные родственники с болезнью Фабри.\n\n"
                "Рекомендуем взять пациента на дальнейшую диагностику независимо от выраженности симптомов. "
                "Для уточнения диагноза необходимо направить пациента на генетический анализ и провести анализ уровня фермента альфа-галактозидазы.\n\n"
                f"Наберите на горячую линию: {HOTLINE_PHONE} и получите диагностический конверт."
            )
        elif diagnostics_needed and wants_callback:
            info = (
                "На основе ваших ответов выявлено сходство некоторых признаков с Болезнью Фабри. "
                "Болезнь Фабри – это редкое генетическое заболевание.\n\n"
                "Для точной диагностики необходимо направить пациента на генетический анализ и провести анализ уровня фермента альфа-галактозидазы.\n\n"
                f"Наберите на горячую линию: {HOTLINE_PHONE} и получите диагностический конверт."
            )
        elif diagnostics_needed:
            info = (
                "На основе ваших ответов выявлено сходство некоторых признаков с Болезнью Фабри. "
                "Болезнь Фабри – это редкое генетическое заболевание.\n\n"
                "Для точной диагностики необходимо направить пациента на генетический анализ и провести анализ уровня фермента альфа-галактозидазы.\n\n"
                f"Наберите на горячую линию: {HOTLINE_PHONE} и получите диагностический конверт."
            )
        else:
            info = (
                "По результатам анкеты выраженных признаков болезни Фабри у пациента не выявлено.\n\n"
                "Если клиническая картина изменится, рассмотрите повторную оценку или дообследование."
            )
    await message.answer(info)

    pdf_kb = InlineKeyboardBuilder()
    pdf_kb.button(text="📄 Получить результаты анкеты в PDF", callback_data="get_pdf")
    await message.answer(
        "Вы можете скачать результаты анкетирования в формате PDF:",
        reply_markup=pdf_kb.as_markup(),
    )

    # Store score in data
    data["fabry_score"] = fabry_score
    data["score_interpretation"] = score_interpretation
    data["score_breakdown"] = score_breakdown

    survey_json = build_survey_result(user_id, chat_id, username, data)
    logger.info(
        "Survey result JSON for user_id=%s:\n%s",
        user_id,
        json.dumps(survey_json, ensure_ascii=False, indent=2),
    )

    user_ident = f"@{username} (ID={user_id})" if username else f"user_id={user_id}"
    contact_parts = [f"callback={'yes' if wants_callback else 'no'}"]
    if not wants_callback:
        contact_parts.append(f"sms={'yes' if _wants_sms(data) else 'no'}")
    contact_pref = " | ".join(contact_parts)

    logger.info(
        "Survey completed for %s | %s | Fabry Risk Score: %s (%s)",
        user_ident,
        contact_pref,
        fabry_score,
        score_interpretation,
    )

    should_forward = is_doctor or fabry_score >= 3
    if should_forward:
        report_text = build_group_report("🩺 Новая анкета", user_id, chat_id, data, username=username)
        await _send_to_group(report_text)

        # Send all collected attachments to group
        additional_payload = data.get("additional_payload", [])
        if additional_payload:
            await _send_attachments_to_group(additional_payload)

    _pdf_data_cache[chat_id] = dict(data)
    await state.clear()


async def finish_with_confirmed_diagnosis(message, state: FSMContext) -> None:
    """Early finish if user already has confirmed Fabry diagnosis."""
    global admin_forwarding_enabled

    data = await state.get_data()
    user_id = message.from_user.id
    chat_id = message.chat.id
    username = message.from_user.username

    await _delete_tracked(chat_id, state)

    await message.answer(
        "Спасибо за ответ. Поскольку у вас уже диагностирована болезнь Фабри, "
        "мы передали информацию специалисту."
    )
    await message.answer(
        "Для дальнейшей консультации и сопровождения свяжитесь с горячей линией:\n"
        f"{HOTLINE_PHONE}"
    )

    pdf_kb = InlineKeyboardBuilder()
    pdf_kb.button(text="📄 Получить результаты анкеты в PDF", callback_data="get_pdf")
    await message.answer(
        "Вы можете скачать результаты анкетирования в формате PDF:",
        reply_markup=pdf_kb.as_markup(),
    )

    data["early_exit_reason"] = "confirmed_fabry_diagnosis"
    answers = data.get("answers", {})
    fabry_score, score_breakdown = calculate_fabry_score_details(answers)
    data["fabry_score"] = fabry_score
    data["score_interpretation"] = get_score_interpretation(fabry_score)
    data["score_breakdown"] = score_breakdown

    survey_json = build_survey_result(user_id, chat_id, username, data)
    logger.info(
        "Survey result JSON (early exit) for user_id=%s:\n%s",
        user_id,
        json.dumps(survey_json, ensure_ascii=False, indent=2),
    )

    report_text = build_group_report(
        "🩺 Анкета завершена досрочно (подтвержденный диагноз Фабри)",
        user_id,
        chat_id,
        data,
        username=username,
    )
    await _send_to_group(report_text)

    # Send all collected attachments to group
    additional_payload = data.get("additional_payload", [])
    if additional_payload:
        await _send_attachments_to_group(additional_payload)

    _pdf_data_cache[chat_id] = dict(data)
    await state.clear()


# =========================
# Handlers (registered in register_handlers())
# =========================

async def cmd_start(message, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(SurveyFSM.waiting_consent)

    welcome = (
        "Здравствуйте!\n\n"
        "Этот бот помогает провести первичное анкетирование. "
        "Мы собираем сведения для организации медицинской помощи и связи со специалистом.\n\n"
        "Пожалуйста, подтвердите согласие на обработку персональных данных (ФЗ-152).\n"
        "Вы можете связаться с оператором по горячей линии на любом этапе."
    )
    await message.answer(welcome, reply_markup=consent_keyboard().as_markup())


async def cb_hotline(callback, state: FSMContext) -> None:
    sent = await callback.message.answer(f"Позвоните нам по номеру: {HOTLINE_PHONE}")
    await _track_msg(state, sent.message_id)
    await callback.answer()


async def cb_consent(callback, state: FSMContext) -> None:
    if callback.data == "consent|no":
        await callback.answer()
        await state.clear()
        await state.set_state(SurveyFSM.waiting_consent)
        reconsent_kb = InlineKeyboardBuilder()
        reconsent_kb.button(text="✅ Хорошо, я даю своё согласие", callback_data="consent|yes")
        reconsent_kb.adjust(1)
        hotline_keyboard_row(reconsent_kb)
        await callback.message.answer(
            "К сожалению, без вашего согласия мы не можем продолжить.\n"
            f"Для получения помощи позвоните на горячую линию: {CONSENT_DECLINE_PHONE}\n\n"
            "Либо, для продолжения использования бота, дайте свое согласие",
            reply_markup=reconsent_kb.as_markup(),
        )
        return

    await callback.answer()
    await state.update_data(
        consent=True,
        consent_timestamp_utc=_utc_iso(),
        answers={},
        additional_payload=[],
        step_index=0,
    )
    thanks = await callback.message.answer("Спасибо! Начинаем анкетирование.")
    await callback.message.answer(
        f"📞 На любом этапе анкетирования вы можете позвонить на горячую линию: {HOTLINE_PHONE}"
    )
    await _track_msg(state, thanks.message_id)
    await send_step(callback.message, state)


async def cb_choice_answer(callback, state: FSMContext) -> None:
    data = await state.get_data()
    parts = (callback.data or "").split("|")
    if len(parts) != 3:
        await callback.answer("Некорректная кнопка.", show_alert=True)
        return

    step_idx = int(parts[1])
    opt_idx = int(parts[2])

    current_idx = data.get("step_index")
    if current_idx != step_idx:
        await callback.answer("Эта кнопка относится к предыдущему вопросу.", show_alert=False)
        return

    step = step_by_index(step_idx)
    opts = step.options(data) if step.options else []
    if not (0 <= opt_idx < len(opts)):
        await callback.answer("Некорректный вариант.", show_alert=True)
        return

    await callback.answer()

    value = opts[opt_idx]
    answers = dict(data.get("answers", {}))
    answers[step.key] = value

    patch: dict[str, Any] = {"answers": answers}
    if step.key in ("role", "sex", "callback_pref", "sms_pref"):
        patch[step.key] = value
    await state.update_data(**patch)

    new_data = await state.get_data()

    if step.key == "fabry_confirmed" and value == "Да":
        await finish_with_confirmed_diagnosis(callback.message, state)
        return

    nxt = next_step_index(step_idx + 1, new_data)
    if nxt is None:
        await finish_survey(callback.message, state)
        return

    await state.update_data(step_index=nxt)
    await send_step(callback.message, state)


async def wrong_input_in_choice(message, state: FSMContext) -> None:
    data = await state.get_data()
    idx = data.get("step_index", 0)
    step = step_by_index(idx)
    opts = step.options(data) if step.options else []
    sent = await message.answer(
        "Пожалуйста, выберите вариант из предложенных кнопок ниже. 👇",
        reply_markup=choice_keyboard(idx, opts).as_markup(),
    )
    await _track_msg(state, message.message_id, sent.message_id)


async def text_answer(message, state: FSMContext) -> None:
    data = await state.get_data()
    idx = data.get("step_index", 0)
    step = step_by_index(idx)

    # Handle shared contact for the phone step (only on Telegram, Max doesn't support it)
    if step.key == "phone" and not message.is_max() and message.contact:
        phone = message.contact.phone_number
        if not phone.startswith("+"):
            phone = f"+{phone}"
        raw = phone
    elif message.text is not None:
        raw = _normalize_spaces(message.text)
    else:
        if step.key == "phone":
            hint = "Пожалуйста, отправьте номер телефона текстом или нажмите «Поделиться номером»."
        else:
            hint = "Пожалуйста, отправьте ответ текстом."
        sent = await message.answer(hint, reply_markup=text_keyboard().as_markup())
        await _track_msg(state, message.message_id, sent.message_id)
        return

    if step.validator:
        ok, err = step.validator(raw, data)
        if not ok:
            sent = await message.answer(err, reply_markup=text_keyboard().as_markup())
            await _track_msg(state, message.message_id, sent.message_id)
            return

    # Track user message so it gets deleted with the next step
    await _track_msg(state, message.message_id)

    answers = dict(data.get("answers", {}))
    answers[step.key] = raw

    patch: dict[str, Any] = {"answers": answers}
    if step.key in ("role", "sex"):
        patch[step.key] = raw
    await state.update_data(**patch)

    # Remove reply keyboard if it was shown (phone step)
    if step.key == "phone":
        rm_msg = await message.answer("✓", reply_markup=ReplyKeyboardRemove())
        await _track_msg(state, rm_msg.message_id)

    new_data = await state.get_data()
    nxt = next_step_index(idx + 1, new_data)
    if nxt is None:
        await finish_survey(message, state)
        return

    await state.update_data(step_index=nxt)
    await send_step(message, state)


async def collect_additional(message, state: FSMContext) -> None:
    data = await state.get_data()
    idx = data.get("step_index", 0)

    payload_item: dict[str, Any] = {"ts_utc": _utc_iso(), "message_id": message.message_id}

    if message.text:
        payload_item["type"] = "text"
        payload_item["text"] = _normalize_spaces(message.text)
    elif message.document:
        payload_item["type"] = "document"
        payload_item["file_id"] = message.document.file_id
        payload_item["file_name"] = message.document.file_name
        payload_item["mime_type"] = message.document.mime_type
    elif message.photo:
        payload_item["type"] = "photo"
        payload_item["file_id"] = message.photo[-1].file_id
    elif message.voice:
        payload_item["type"] = "voice"
        payload_item["file_id"] = message.voice.file_id
    elif message.audio:
        payload_item["type"] = "audio"
        payload_item["file_id"] = message.audio.file_id
        payload_item["file_name"] = message.audio.file_name
    else:
        payload_item["type"] = "other"
        payload_item["note"] = "Unsupported content type"

    additional = list(data.get("additional_payload", []))
    additional.append(payload_item)
    await state.update_data(additional_payload=additional)

    sent = await message.answer(
        "Добавлено. Можете отправить еще сообщения или нажать «✅ Продолжить».",
        reply_markup=collect_keyboard(idx).as_markup(),
    )
    await _track_msg(state, message.message_id, sent.message_id)


async def cb_collect_done(callback, state: FSMContext) -> None:
    data = await state.get_data()
    parts = (callback.data or "").split("|")
    if len(parts) != 2:
        await callback.answer("Некорректная кнопка.", show_alert=True)
        return

    step_idx = int(parts[1])
    current_idx = data.get("step_index")
    if current_idx != step_idx:
        await callback.answer("Эта кнопка относится к предыдущему шагу.", show_alert=False)
        return

    await callback.answer()

    additional_payload = data.get("additional_payload", [])
    answers = dict(data.get("answers", {}))
    answers["additional_info"] = f"{len(additional_payload)} item(s)"
    await state.update_data(answers=answers)

    new_data = await state.get_data()
    nxt = next_step_index(step_idx + 1, new_data)
    if nxt is None:
        await finish_survey(callback.message, state)
        return

    await state.update_data(step_index=nxt)
    await send_step(callback.message, state)


async def cb_get_pdf(callback) -> None:
    chat_id = callback.message.chat.id
    data = _pdf_data_cache.get(chat_id)
    if not data:
        await callback.answer(
            "Результаты не найдены. Пройдите анкету заново (/start).",
            show_alert=True,
        )
        return
    await callback.answer("Генерация PDF...")
    try:
        pdf_bytes = generate_pdf_report(data)
        pdf_file = BufferedInputFile(pdf_bytes, filename="fabry_screening_results.pdf")
        await callback.message.answer_document(pdf_file, caption="Результаты анкетирования")
    except Exception:
        logger.exception("Failed to generate PDF for chat %s", chat_id)
        await callback.message.answer("Произошла ошибка при генерации PDF. Попробуйте позже.")


async def cb_fallback(callback) -> None:
    await callback.answer("Эта кнопка больше не актуальна.", show_alert=False)


async def message_fallback(message) -> None:
    await message.answer("Чтобы начать анкетирование, отправьте команду /start")


# =========================
# Register Handlers
# =========================

def register_handlers() -> None:
    """Register all handlers with the router. Must be called after router initialization."""
    router.message(CommandStart())(cmd_start)
    router.callback_query(F.data == "hotline")(cb_hotline)
    router.callback_query(F.data.startswith("consent|"))(cb_consent)
    router.callback_query(SurveyFSM.waiting_choice, F.data.startswith("ans|"))(cb_choice_answer)
    router.message(SurveyFSM.waiting_choice)(wrong_input_in_choice)
    router.message(SurveyFSM.waiting_text)(text_answer)
    router.message(SurveyFSM.collecting_additional)(collect_additional)
    router.callback_query(SurveyFSM.collecting_additional, F.data.startswith("collect_done|"))(cb_collect_done)
    router.callback_query(F.data == "get_pdf")(cb_get_pdf)
    router.callback_query()(cb_fallback)
    router.message()(message_fallback)


# =========================
# Main
# =========================

async def main() -> None:
    global bot, dp, router

    bot, dp, router = create_bot(
        tg_token=BOT_TOKEN or None,
        max_token=MAX_BOT_TOKEN or None,
        fsm_storage=MemoryStorage()
    )

    # Register all handlers after router is initialized
    register_handlers()

    # Set up TelegramLogHandler only if Telegram token is available
    if BOT_TOKEN and LOG_CHAT_ID:
        try:
            tg_bot = bot.get_bot(BPlatform.telegram)
            telegram_log_handler = TelegramLogHandler(tg_bot, LOG_CHAT_ID)
            telegram_log_handler.setFormatter(
                logging.Formatter("[Лог %(levelname)s] %(asctime)s\n%(message)s")
            )
            logger.addHandler(telegram_log_handler)
        except Exception:
            logger.warning("Could not set up Telegram log handler")

    retry_delay = 1.0
    while True:
        try:
            logger.info(
                "Starting bot polling | test_mode=%s | tg_token=%s | max_token=%s | tg_group=%s | max_group=%s | log_chat=%s",
                TEST_MODE,
                bool(BOT_TOKEN),
                bool(MAX_BOT_TOKEN),
                GROUP_CHAT_ID,
                MAX_GROUP_CHAT_ID,
                LOG_CHAT_ID,
            )
            await dp.start_polling(bot)
            break
        except TelegramNetworkError as exc:
            logger.warning(
                "Polling network error: %s. Retrying in %.1f sec",
                exc,
                retry_delay,
            )
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 1.8, 60.0)
        except Exception:
            logger.exception("Unexpected polling error. Retrying in %.1f sec", retry_delay)
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 1.8, 60.0)


if __name__ == "__main__":
    if not BOT_TOKEN and not MAX_BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN or MAX_BOT_TOKEN is required. Put them into .env")
    asyncio.run(main())
