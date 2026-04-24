from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Literal, Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv
from fpdf import FPDF


# =========================
# Configuration
# =========================

load_dotenv()

TEST_MODE = os.getenv("TEST_MODE", "0").strip().lower() in {"1", "true", "yes", "on"}
TEST_BOT_TOKEN = os.getenv("TEST_BOT_TOKEN", "").strip()
TEST_GROUP_CHAT_ID_RAW = os.getenv("TEST_GROUP_CHAT_ID", "").strip()

BOT_TOKEN = TEST_BOT_TOKEN if TEST_MODE else os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN and __name__ == "__main__":
    raise RuntimeError("BOT_TOKEN is not set. Put it into .env")

# Phone number shown when user taps the hotline button (requirement #3)
HOTLINE_PHONE = os.getenv("HOTLINE_PHONE", "+7 (495) 123-45-67").strip()

# Phone number shown when user declines processing consent (requirement #1)
CONSENT_DECLINE_PHONE = os.getenv("CONSENT_DECLINE_PHONE", HOTLINE_PHONE).strip()

# Group chat where completed surveys and logs are forwarded (bot must be a member)
# Example: GROUP_CHAT_ID=-1001234567890
GROUP_CHAT_ID_RAW = (
    TEST_GROUP_CHAT_ID_RAW if TEST_MODE else os.getenv("GROUP_CHAT_ID", "").strip()
)
GROUP_CHAT_ID: Optional[int] = int(GROUP_CHAT_ID_RAW) if GROUP_CHAT_ID_RAW else None
LOG_CHAT_ID_RAW = (
    TEST_GROUP_CHAT_ID_RAW if TEST_MODE else os.getenv("LOG_CHAT_ID", "").strip()
)
LOG_CHAT_ID: Optional[int] = int(LOG_CHAT_ID_RAW) if LOG_CHAT_ID_RAW else GROUP_CHAT_ID
FABRY_SCORE_WEIGHTS_PATH = os.path.join(
    os.path.dirname(__file__), "fabry_score_weights.json"
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

    def emit(self, record: logging.LogRecord) -> None:
        if not bot or not LOG_CHAT_ID:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        message = self.format(record)
        loop.create_task(_send_log_to_group(message))


async def _send_log_to_group(text: str) -> None:
    if not bot or not LOG_CHAT_ID:
        return
    try:
        # Telegram allows up to 4096 chars per message.
        await bot.send_message(LOG_CHAT_ID, text[:4000])
    except Exception:
        # Do not recursively log handler errors.
        pass


# =========================
# Global bot instance (initialized in main())
# =========================

bot: Optional[Bot] = None
admin_forwarding_enabled = True
_pdf_data_cache: dict[int, dict[str, Any]] = {}

class SurveyFSM(StatesGroup):
    waiting_consent = State()
    waiting_choice = State()
    waiting_text = State()
    collecting_additional = State()


# =========================
# Questionnaire model
# =========================

Condition = Callable[[dict[str, Any]], bool]
TextFn = Callable[[dict[str, Any]], str]
OptionsFn = Callable[[dict[str, Any]], list[str]]
Validator = Callable[[str, dict[str, Any]], tuple[bool, str]]  # ok, error_message


def _always(_: dict[str, Any]) -> bool:
    return True


@dataclass(frozen=True)
class Step:
    key: str
    kind: Literal["choice", "text", "collect"]
    text: TextFn
    options: Optional[OptionsFn] = None
    condition: Condition = _always
    validator: Optional[Validator] = None


# =========================
# Helpers (validation, keyboards, flow)
# =========================

def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_spaces(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip())


def validate_age(text: str, _: dict[str, Any]) -> tuple[bool, str]:
    # Strict validation: only digits allowed
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
    t = _normalize_spaces(text)
    if len(t) < 2:
        return False, "Пожалуйста, укажите ваше ФИО."
    # Only letters, spaces, hyphens, dots allowed
    if not re.match(r"^[А-Яа-яЁёA-Za-z\s.\-]+$", t):
        return False, "ФИО может содержать только буквы, пробелы, дефисы и точки."
    return True, ""


def validate_phone(text: str, _: dict[str, Any]) -> tuple[bool, str]:
    t = _normalize_spaces(text)
    # Allow only digits, +, -, (, ), spaces
    if not re.match(r"^[\d\s+\-()\,]+$", t):
        return False, "Номер телефона содержит недопустимые символы. Используйте только цифры, +, -, (, )."
    digits = re.sub(r"\D", "", t)
    if len(digits) < 10:
        return False, "Не вижу номера телефона. Можно в формате +7XXXXXXXXXX или 8XXXXXXXXXX."
    if len(digits) > 15:
        return False, "Слишком длинный номер. Проверьте, пожалуйста."
    return True, ""


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
        # callback_data must be 1–64 bytes (Telegram Bot API limitation)
        kb.button(text=opt, callback_data=f"ans|{step_index}|{i}")
    # One option per row for accessibility and to avoid very wide keyboards.
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


async def get_data(state: FSMContext) -> dict[str, Any]:
    return await state.get_data()


async def set_step_index(state: FSMContext, idx: int) -> None:
    await state.update_data(step_index=idx)


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


async def save_answer(state: FSMContext, key: str, value: Any) -> None:
    data = await get_data(state)
    answers = dict(data.get("answers", {}))
    answers[key] = value
    await state.update_data(answers=answers)


def _step_text_role(_: dict[str, Any]) -> str:
    return "Кто заполняет анкету?"


def _step_options_role(_: dict[str, Any]) -> list[str]:
    return ["Пациент", "Врач"]


def _step_text_sex(data: dict[str, Any]) -> str:
    return _for_patient_or_self(
        data,
        "Укажите, пожалуйста, ваш пол:",
        "Укажите, пожалуйста, пол пациента:",
    )


def _step_options_sex(_: dict[str, Any]) -> list[str]:
    return ["Мужской", "Женский"]


def _for_patient_or_self(data: dict[str, Any], self_text: str, patient_text: str) -> str:
    """Use patient-oriented wording when survey is filled by a doctor."""
    return patient_text if _is_doctor(data) else self_text


def _step_text_age(data: dict[str, Any]) -> str:
    return _for_patient_or_self(
        data,
        "Укажите Ваш возраст (числом)\n\n"
        "Важно: у мужчин симптомы проявляются раньше и тяжелее, у женщин – вариабельно",
        "Укажите возраст пациента (числом)\n\n"
        "Важно: у мужчин симптомы проявляются раньше и тяжелее, у женщин – вариабельно",
    )


def _step_text_genetic(data: dict[str, Any]) -> str:
    return _for_patient_or_self(
        data,
        "Есть ли у Вас генетически подтвержденный диагноз Болезнь Фабри?",
        "Есть ли у вашего пациента генетически подтвержденный диагноз Болезнь Фабри?",
    )


def _opts_yes_no(_: dict[str, Any]) -> list[str]:
    return ["Да", "Нет"]


def _opts_yes_no_dk(_: dict[str, Any]) -> list[str]:
    return ["Да", "Нет", "Не знаю"]


def _step_text_relatives_dx(data: dict[str, Any]) -> str:
    return _for_patient_or_self(
        data,
        "Есть ли у вас кровные родственники с диагностированной Болезнью Фабри?",
        "Есть ли у вашего пациента кровные родственники с диагностированной Болезнью Фабри?",
    )


def _step_text_relatives_kidney_heart_stroke(data: dict[str, Any]) -> str:
    return _for_patient_or_self(
        data,
        "Есть ли у вас кровные родственники с заболеваниями почек, "
        "с заболеваниями сердца, перенесшие инсульт в молодом возрасте (до 50 лет)?",
        "Есть ли у вашего пациента кровные родственники с заболеваниями почек, "
        "с заболеваниями сердца, перенесшие инсульт в молодом возрасте (до 50 лет)?",
    )


def _step_text_pain(data: dict[str, Any]) -> str:
    return _for_patient_or_self(
        data,
        "Боли в руках и ногах\n"
        "Важно: это один из самых ранних признаков\n\n"
        "Испытываете ли вы эпизодические или постоянные боли (жжение, покалывание, онемение) в ладонях и стопах?",
        "Неврологические и болевые синдромы (Акропарестезии)\n"
        "Важно: это один из самых ранних признаков\n\n"
        "Испытывает ли ваш пациент эпизодические или постоянные боли (жжение, покалывание, онемение) в ладонях и стопах?",
    )


def _step_opts_pain(_: dict[str, Any]) -> list[str]:
    return ["Да", "Никогда"]


def _step_text_crises(data: dict[str, Any]) -> str:
    return _for_patient_or_self(
        data,
        "Усиливаются ли эти боли при физической нагрузке, смене погоды, стрессе "
        "или после горячей ванны (бывают ли такие болевые приступы)?",
        "Усиливаются ли у пациента эти боли при физической нагрузке, смене погоды, стрессе "
        "или после горячей ванны (так называемые «кризы Фабри»)?",
    )


def _step_text_sweating(data: dict[str, Any]) -> str:
    return _for_patient_or_self(
        data,
        "Бывает ли у вас сниженное потоотделение? Например, вы почти не потеете "
        "в спортзале или в жару, перегреваетесь?",
        "Бывает ли у вашего пациента сниженное потоотделение (гипогидроз)? Например, пациент почти не потеет "
        "в спортзале или в жару, перегревается?",
    )


def _step_opts_sweating(_: dict[str, Any]) -> list[str]:
    return ["Да", "Нет"]


def _step_text_gi(data: dict[str, Any]) -> str:
    return _for_patient_or_self(
        data,
        "Желудочно-кишечный тракт\n\n"
        "Беспокоят ли вас вздутие живота, диарея или боли в животе",
        "Желудочно-кишечный тракт\n\n"
        "Беспокоят ли пациента вздутие живота, диарея или боли в животе"
    )


def _step_opts_gi(_: dict[str, Any]) -> list[str]:
    return ["Нет", "Иногда", "Регулярно, с детства"]


def _step_text_satiety(data: dict[str, Any]) -> str:
    return _for_patient_or_self(
        data,
        "Ощущаете ли вы чувство быстрого насыщения (наедаетесь маленькой порцией)?",
        "Ощущает ли пациент чувство быстрого насыщения (наедается маленькой порцией)?",
    )


def _step_opts_satiety(_: dict[str, Any]) -> list[str]:
    return ["Да, часто", "Иногда", "Нет"]


def _step_text_skin(data: dict[str, Any]) -> str:
    return _for_patient_or_self(
        data,
        "Кожа\n\n"
        "Замечали ли вы у себя небольшие (1-3 мм) темно-красные, почти черные "
        "пятна на коже, выступающие над поверхностность кожи, не вызывающие дискомфорта, не исчезающие при надавливании на элемент, особенно в области:",
        "Кожа (Ангиокератомы)\n\n"
        "Наблюдаются ли у пациента небольшие (1-3 мм) темно-красные, почти черные, "
        "пятна на коже, выступающие над поверхностность кожи, не вызывающие дискомфорта, не исчезающие при надавливании на элемент, особенно в области:",
    )


def _step_opts_skin(_: dict[str, Any]) -> list[str]:
    return [
        "Между пупком и коленями («зона плавок»)",
        "На бедрах, ягодицах, в паху",
        "На губах и слизистой рта",
        "Нет, не замечал(а)",
    ]


def _step_text_tachy(data: dict[str, Any]) -> str:
    return _for_patient_or_self(
        data,
        "Сердечно-сосудистая система\n\n"
        "Бывает ли у вас учащенное сердцебиение или перебои в работе сердца "
        "вне физической нагрузки?",
        "Сердечно-сосудистая система\n\n"
        "Бывает ли у пациента учащенное сердцебиение (тахикардия) или перебои в работе сердца "
        "вне физической нагрузки?",
    )


def _step_text_heart_enlargement(data: dict[str, Any]) -> str:
    return _for_patient_or_self(
        data,
        "Если проходили исследование, Вам ставили увеличение объемов сердца?",
        "Пациенту ставили увеличение объемов сердца (ГКМП)?",
    )


def _step_text_dyspnea(data: dict[str, Any]) -> str:
    return _for_patient_or_self(
        data,
        "Есть ли у вас одышка при привычных нагрузках (подъем по лестнице), которая "
        "не объясняется лишним весом?",
        "Есть ли у пациента одышка при привычных нагрузках (подъем по лестнице), которая "
        "не объясняется лишним весом?",
    )


def _step_text_edema(data: dict[str, Any]) -> str:
    return _for_patient_or_self(
        data,
        "Почки\n\nЕсть ли у вас отеки (ног, под глазами) по утрам?",
        "Почки\n\nЕсть ли у пациента отеки (ног, под глазами) по утрам?",
    )


def _step_text_proteinuria(data: dict[str, Any]) -> str:
    return _for_patient_or_self(
        data,
        "Знаете ли вы свой уровень белка в моче или креатинин "
        "(были выявлены отклонения в анализах мочи или крови)?",
        "Известны ли показатели пациента по белку в моче (протеинурия) или креатинину "
        "(были выявлены отклонения в анализах мочи или крови)?",
    )


def _step_opts_proteinuria(_: dict[str, Any]) -> list[str]:
    return ["Да, были отклонения", "Нет, все в норме", "Не проверял(а)"]


def _step_text_hearing(data: dict[str, Any]) -> str:
    return _for_patient_or_self(
        data,
        "Слух и равновесие\n\n"
        "Замечали ли вы снижение слуха или шум в ушах?",
        "Слух и вестибулярный аппарат\n\n"
        "Наблюдаются ли у пациента снижение слуха или шум в ушах?",
    )


def _step_opts_hearing(_: dict[str, Any]) -> list[str]:
    return ["Да (с молодости)", "Да (с возрастом)", "Нет"]


def _step_text_dizziness(data: dict[str, Any]) -> str:
    return _for_patient_or_self(
        data,
        "Сопровождаются ли эти симптомы (или бывают ли отдельно) приступами сильного "
        "головокружения, ощущения неустойчивости?",
        "Сопровождаются ли эти симптомы у пациента (или бывают ли отдельно) приступами сильного "
        "головокружения, ощущения неустойчивости?",
    )


def _step_text_eyes(data: dict[str, Any]) -> str:
    return _for_patient_or_self(
        data,
        "Глаза\n\n"
        "Говорили ли вам офтальмологи о наличии специфических изменений роговицы "
        "(например, помутнение роговицы) "
        "или изменении сосудов глазного дна?",
        "Глаза (Специфический признак)\n\n"
        "Сообщали ли офтальмологи о наличии у пациента специфических поражений роговицы "
        "(так называемая «вихревидная кератопатия» или помутнение роговицы) "
        "или изменении сосудов глазного дна?",
    )


def _step_text_stroke_tia(data: dict[str, Any]) -> str:
    return _for_patient_or_self(
        data,
        "Были ли у вас ранее зафиксированы случаи инсультов или транзиторных ишемических атак (ТИА)?",
        "Были ли у пациента ранее зафиксированы случаи инсультов или транзиторных ишемических атак (ТИА)?",
    )


def _step_text_myocardial_infarction(data: dict[str, Any]) -> str:
    return _for_patient_or_self(
        data,
        "Были ли у вас ранее зафиксированы случаи инфаркта миокарда?",
        "Были ли у пациента ранее зафиксированы случаи инфаркта миокарда?",
    )


def _step_text_chronic_kidney_disease(data: dict[str, Any]) -> str:
    return _for_patient_or_self(
        data,
        "Имеется ли у вас хроническая болезнь почек (ХБП)?",
        "Имеется ли у пациента хроническая болезнь почек (ХБП)?",
    )


def _step_opts_eyes(_: dict[str, Any]) -> list[str]:
    return ["Да, находили", "Нет, не находили", "Не помню", "Не проверял глаза"]


def _step_text_city(data: dict[str, Any]) -> str:
    return _for_patient_or_self(
        data,
        "Укажите пожалуйста Ваш город",
        "Укажите, пожалуйста, Ваш город.",
    )


def _step_text_spec(_: dict[str, Any]) -> str:
    return "Укажите пожалуйста Вашу специализацию и должность?"


def _step_text_workplace(_: dict[str, Any]) -> str:
    return "Укажите пожалуйста место работы?"


def _is_doctor(data: dict[str, Any]) -> bool:
    # Prefer role from answers (source of truth) and fall back to top-level cache.
    answers = data.get("answers", {})
    role_from_answers = _normalize_spaces(str(answers.get("role", "")))
    role_from_state = _normalize_spaces(str(data.get("role", "")))
    role = role_from_answers or role_from_state
    return role.startswith("Врач")


def _is_patient(data: dict[str, Any]) -> bool:
    return not _is_doctor(data)


def _has_no_fabry_diagnosis(data: dict[str, Any]) -> bool:
    """Return True if detailed medical questions should be asked.
    Skip medical questions if Fabry is already confirmed."""
    answers = data.get("answers", {})
    return answers.get("fabry_confirmed") != "Да"


def _step_text_additional(data: dict[str, Any]) -> str:
    return _for_patient_or_self(
        data,
        "Имеются ли у вас дополнительные сведения, результаты анализов, которые вы хотите указать?\n\n"
        "Можно отправить несколько сообщений: текст, фото, документы.\n"
        "Когда закончите — нажмите «✅ Продолжить».",
        "Есть ли дополнительные сведения или результаты анализов пациента, которые нужно указать?\n\n"
        "Можно отправить несколько сообщений: текст, фото, документы.\n"
        "Когда закончите — нажмите «✅ Продолжить».",
    )


def _step_text_callback_pref(data: dict[str, Any]) -> str:
    return _for_patient_or_self(
        data,
        "Хотите ли вы, чтобы специалист перезвонил Вам по результатам анкеты?",
        "Нужно ли, чтобы специалист перезвонил Вам по результатам анкеты?",
    )


def _step_opts_callback_pref(_: dict[str, Any]) -> list[str]:
    return ["Да, я жду обратного звонка", "Нет, звонок не нужен"]


def _wants_callback(data: dict[str, Any]) -> bool:
    return data.get("callback_pref") == "Да, я жду обратного звонка"


def _wants_sms(data: dict[str, Any]) -> bool:
    return data.get("sms_pref") == "Да, хочу получить рекомендацию в СМС"


def _should_ask_sms_pref(data: dict[str, Any]) -> bool:
    return _is_patient(data) and not _wants_callback(data)


def _step_text_full_name(data: dict[str, Any]) -> str:
    if _is_doctor(data):
        return "Укажите, пожалуйста, ваше, врача, ФИО (Фамилия Имя Отчество)."
    return "Укажите пожалуйста ваше ФИО (Фамилия Имя Отчество):"


def _step_text_phone(data: dict[str, Any]) -> str:
    if _is_doctor(data):
        return (
            "Укажите, пожалуйста, ваш контактный номер телефона.\n"
            "Пример: +7XXXXXXXXXX\n\n"
            "Или нажмите кнопку «Поделиться номером» ниже."
        )
    return (
        "Укажите пожалуйста ваш номер телефона.\n"
        "Пример: +7XXXXXXXXXX\n\n"
        "Или нажмите кнопку «Поделиться номером» ниже."
    )


def _should_ask_phone(data: dict[str, Any]) -> bool:
    if _is_doctor(data):
        return True
    return _wants_callback(data) or _wants_sms(data)


def _should_ask_pain_triggers(data: dict[str, Any]) -> bool:
    """Show pain triggers question only if user has pain (not "Никогда")."""
    answers = data.get("answers", {})
    pain = answers.get("pain_hands_feet")
    return pain is not None and pain != "Никогда" and pain != "Нет"


HOTLINE_REMINDERS: dict[int, str] = {
    6: (
        "📞 Напоминаем: если у вас возникнут вопросы, вы можете в любой момент "
        f"позвонить на горячую линию: {HOTLINE_PHONE}"
    ),
    21: (
        "📞 Медицинская часть анкеты завершена. Если вы хотите обсудить "
        f"результаты со специалистом — звоните на горячую линию: {HOTLINE_PHONE}"
    ),
}

STEPS: list[Step] = [
    Step(key="role", kind="choice", text=_step_text_role, options=_step_options_role),
    Step(key="sex", kind="choice", text=_step_text_sex, options=_step_options_sex),
    Step(key="age", kind="text", text=_step_text_age, validator=validate_age),
    Step(key="fabry_confirmed", kind="choice", text=_step_text_genetic, options=_opts_yes_no),
    # Medical questions - skip if Fabry is already confirmed
    Step(key="relatives_fabry", kind="choice", text=_step_text_relatives_dx, options=_opts_yes_no_dk, condition=_has_no_fabry_diagnosis),
    Step(key="relatives_kidney_heart_stroke", kind="choice", text=_step_text_relatives_kidney_heart_stroke, options=_opts_yes_no_dk, condition=_has_no_fabry_diagnosis),
    Step(key="pain_hands_feet", kind="choice", text=_step_text_pain, options=_step_opts_pain, condition=_has_no_fabry_diagnosis),
    Step(key="pain_triggers", kind="choice", text=_step_text_crises, options=_opts_yes_no, condition=lambda d: _has_no_fabry_diagnosis(d) and _should_ask_pain_triggers(d)),
    Step(key="sweating", kind="choice", text=_step_text_sweating, options=_step_opts_sweating, condition=_has_no_fabry_diagnosis),
    Step(key="gi_after_meals", kind="choice", text=_step_text_gi, options=_step_opts_gi, condition=_has_no_fabry_diagnosis),
    Step(key="early_satiety", kind="choice", text=_step_text_satiety, options=_step_opts_satiety, condition=_has_no_fabry_diagnosis),
    Step(key="angiokeratomas", kind="choice", text=_step_text_skin, options=_step_opts_skin, condition=_has_no_fabry_diagnosis),
    Step(key="tachycardia", kind="choice", text=_step_text_tachy, options=_opts_yes_no, condition=_has_no_fabry_diagnosis),
    Step(key="heart_enlargement", kind="choice", text=_step_text_heart_enlargement, options=_opts_yes_no, condition=_has_no_fabry_diagnosis),
    Step(key="dyspnea", kind="choice", text=_step_text_dyspnea, options=_opts_yes_no, condition=_has_no_fabry_diagnosis),
    Step(key="myocardial_infarction", kind="choice", text=_step_text_myocardial_infarction, options=_opts_yes_no, condition=_has_no_fabry_diagnosis),
    Step(key="edema", kind="choice", text=_step_text_edema, options=_opts_yes_no, condition=_has_no_fabry_diagnosis),
    Step(key="proteinuria_creatinine", kind="choice", text=_step_text_proteinuria, options=_step_opts_proteinuria, condition=_has_no_fabry_diagnosis),
    Step(key="chronic_kidney_disease", kind="choice", text=_step_text_chronic_kidney_disease, options=_opts_yes_no, condition=_has_no_fabry_diagnosis),
    Step(key="hearing_tinnitus", kind="choice", text=_step_text_hearing, options=_step_opts_hearing, condition=_has_no_fabry_diagnosis),
    Step(key="dizziness", kind="choice", text=_step_text_dizziness, options=_opts_yes_no, condition=_has_no_fabry_diagnosis),
    Step(key="eye_sign", kind="choice", text=_step_text_eyes, options=_step_opts_eyes, condition=_has_no_fabry_diagnosis),
    Step(key="stroke_tia_history", kind="choice", text=_step_text_stroke_tia, options=_opts_yes_no, condition=_has_no_fabry_diagnosis),
    # Location and professional info
    Step(key="city", kind="text", text=_step_text_city, validator=validate_nonempty),
    Step(key="specialization_position", kind="text", text=_step_text_spec, condition=_is_doctor, validator=validate_nonempty),
    Step(key="workplace", kind="text", text=_step_text_workplace, condition=_is_doctor, validator=validate_nonempty),
    Step(key="additional_info", kind="collect", text=_step_text_additional, condition=_is_patient),
    Step(key="callback_pref", kind="choice", text=_step_text_callback_pref, options=_step_opts_callback_pref, condition=_is_patient),
    Step(
        key="sms_pref",
        kind="choice",
        text=lambda _: "Хотели бы вы получить рекомендацию от специалиста в СМС?",
        options=lambda _: ["Да, хочу получить рекомендацию в СМС", "Нет, не нужно"],
        condition=_should_ask_sms_pref,
    ),
    # Contact info - for doctors always ask doctor full name and contact phone
    Step(key="full_name", kind="text", text=_step_text_full_name, validator=validate_full_name),
    Step(key="phone", kind="text", text=_step_text_phone, condition=_should_ask_phone, validator=validate_phone),
]


QUESTION_LABELS: dict[str, str] = {
    "role": "Кто заполняет анкету",
    "sex": "Пол",
    "age": "Возраст",
    "fabry_confirmed": "Подтвержденный диагноз Фабри",
    "relatives_fabry": "Родственники с болезнью Фабри",
    "relatives_kidney_heart_stroke": "Родственники с почечными/сердечными заболеваниями или ранним инсультом",
    "pain_hands_feet": "Боли в ладонях и стопах",
    "pain_triggers": "Усиление болей (кризы Фабри)",
    "sweating": "Потоотделение",
    "gi_after_meals": "ЖКТ симптомы после еды",
    "early_satiety": "Быстрое насыщение",
    "angiokeratomas": "Ангиокератомы",
    "tachycardia": "Тахикардия/перебои в сердце",
    "heart_enlargement": "Увеличение объемов сердца (ГКМП)",
    "dyspnea": "Одышка",
    "myocardial_infarction": "Инфаркт миокарда в анамнезе",
    "edema": "Отеки",
    "proteinuria_creatinine": "Протеинурия/креатинин",
    "chronic_kidney_disease": "Хроническая болезнь почек (ХБП)",
    "hearing_tinnitus": "Слух/тиннитус",
    "dizziness": "Головокружение",
    "eye_sign": "Офтальмологические признаки",
    "stroke_tia_history": "Инсульт/ТИА в анамнезе",
    "city": "Город",
    "specialization_position": "Специализация и должность",
    "workplace": "Место работы",
    "additional_info": "Дополнительные сведения",
    "callback_pref": "Запрос на обратный звонок",
    "sms_pref": "Запрос на СМС рекомендацию",
    "full_name": "ФИО",
    "phone": "Телефон",
}

NOSOLOGY_BLOCKS: list[tuple[str, list[str]]] = [
    ("Общие данные", ["sex", "age", "city"]),
    (
        "Генетика и семейный анамнез",
        ["fabry_confirmed", "relatives_fabry", "relatives_kidney_heart_stroke"],
    ),
    ("Неврология", ["pain_hands_feet", "pain_triggers", "sweating"]),
    ("Желудочно-кишечный тракт", ["gi_after_meals", "early_satiety"]),
    ("Дерматология", ["angiokeratomas"]),
    ("Кардиология", ["tachycardia", "heart_enlargement", "dyspnea", "myocardial_infarction"]),
    ("Нефрология", ["edema", "proteinuria_creatinine", "chronic_kidney_disease"]),
    ("ЛОР и вестибулярные симптомы", ["hearing_tinnitus", "dizziness"]),
    ("Офтальмология", ["eye_sign"]),
    ("Сосудистые события", ["stroke_tia_history"]),
    (
        "Профиль врача",
        ["specialization_position", "workplace"],
    ),
    (
        "Обратная связь и контакты",
        ["callback_pref", "sms_pref", "full_name", "phone"],
    ),
    ("Дополнительные сведения", ["additional_info"]),
]


def _load_fabry_score_rules() -> dict[str, dict[str, float]]:
    try:
        with open(FABRY_SCORE_WEIGHTS_PATH, encoding="utf-8") as fh:
            raw_rules = json.load(fh)
    except OSError as exc:
        raise RuntimeError(
            f"Failed to read score weights file: {FABRY_SCORE_WEIGHTS_PATH}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Invalid JSON in score weights file: {FABRY_SCORE_WEIGHTS_PATH}"
        ) from exc

    if not isinstance(raw_rules, dict):
        raise RuntimeError("Score weights root must be a JSON object.")

    rules: dict[str, dict[str, float]] = {}
    for question_key, option_scores in raw_rules.items():
        if isinstance(question_key, str) and question_key.startswith("_"):
            continue

        if not isinstance(question_key, str) or not isinstance(option_scores, dict):
            raise RuntimeError("Each score rule must map a question key to an object.")

        normalized_scores: dict[str, float] = {}
        for option_value, points in option_scores.items():
            if isinstance(option_value, str) and option_value.startswith("_"):
                continue

            if not isinstance(option_value, str) or not isinstance(points, (int, float)):
                raise RuntimeError(
                    "Each score rule must map string answers to numeric weights."
                )
            normalized_scores[option_value] = float(points)

        rules[question_key] = normalized_scores

    return rules


FABRY_SCORE_RULES = _load_fabry_score_rules()


def next_step_index(start_from: int, data: dict[str, Any]) -> Optional[int]:
    for i in range(start_from, len(STEPS)):
        if STEPS[i].condition(data):
            return i
    return None


def step_by_index(idx: int) -> Step:
    return STEPS[idx]


async def send_step(message: Message, state: FSMContext) -> None:
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

    # For the phone step, also show a reply keyboard with "Share contact" button
    if step.key == "phone":
        share_msg = await message.answer(
            "Или нажмите кнопку ниже, чтобы поделиться номером:",
            reply_markup=phone_reply_keyboard(),
        )
        track_ids.append(share_msg.message_id)

    await _track_msg(state, *track_ids)


def format_summary(data: dict[str, Any]) -> str:
    answers = data.get("answers", {})
    lines: list[str] = []

    role = answers.get("role")
    if role:
        lines.append(f"Роль: {role}")

    if data.get("doctor_followup_reason") == "family_history_fabry":
        lines.append("ВАЖНО: рекомендация выдана по семейному анамнезу")

    score = data.get("fabry_score")
    score_text = data.get("score_interpretation")
    if score is not None and score_text:
        lines.append(f"Оценка риска Фабри: {score} ({score_text})")

    score_breakdown = data.get("score_breakdown", [])
    if score_breakdown:
        lines.append("Сработавший скоринг:")
        for item in score_breakdown:
            lines.append(f"- {item['label']}: {item['answer']} (+{item['points']})")

    for title, keys in NOSOLOGY_BLOCKS:
        block_lines: list[str] = []
        for key in keys:
            if key in answers:
                block_lines.append(f"- {QUESTION_LABELS.get(key, key)}: {answers[key]}")
        if block_lines:
            lines.append(f"\n{title}:")
            lines.extend(block_lines)

    early_exit_reason = data.get("early_exit_reason")
    if early_exit_reason:
        lines.append(f"- Досрочное завершение: {early_exit_reason}")

    additional = data.get("additional_payload", [])
    if additional:
        by_type: dict[str, int] = {}
        for item in additional:
            item_type = item.get("type", "other")
            by_type[item_type] = by_type.get(item_type, 0) + 1

        lines.append(f"- Доп. материалы: {len(additional)}")
        lines.append(
            "  " + ", ".join(f"{k}: {v}" for k, v in sorted(by_type.items()))
        )

    return "\n".join(lines)[:3800]


def build_group_report(title: str, user_id: int, chat_id: int, data: dict[str, Any], username: Optional[str] = None) -> str:
    user_display = f"@{username} (ID: {user_id})" if username else str(user_id)
    report = (
        f"{title}\n"
        f"Пользователь: {user_display}\n"
        f"Чат: {chat_id}\n\n"
        f"{format_summary(data)}"
    )
    return report[:4000]


def build_survey_result(
    user_id: int,
    chat_id: int,
    username: Optional[str],
    data: dict[str, Any],
) -> dict[str, Any]:
    """Build a structured JSON-serializable dict with all survey results.

    This payload is intended for future forwarding to an external service.
    """
    answers = data.get("answers", {})
    score = data.get("fabry_score")
    score_interpretation = data.get("score_interpretation")
    score_breakdown = data.get("score_breakdown", [])

    result: dict[str, Any] = {
        "timestamp_utc": _utc_iso(),
        "user_id": user_id,
        "chat_id": chat_id,
        "username": username,
        "role": answers.get("role"),
        "early_exit_reason": data.get("early_exit_reason"),
        "fabry_score": score,
        "score_interpretation": score_interpretation,
        "score_breakdown": score_breakdown,
        "answers": answers,
        "additional_payload": data.get("additional_payload", []),
    }
    return result


def _find_dejavu_font() -> str:
    """Find DejaVuSans.ttf on the system."""
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/TTF/DejaVuSans.ttf",
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
        os.path.join(os.path.dirname(__file__), "DejaVuSans.ttf"),
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    raise FileNotFoundError(
        "DejaVuSans.ttf not found. Install fonts-dejavu-core or place the font next to main.py."
    )


def generate_pdf_report(data: dict[str, Any]) -> bytes:
    """Generate a PDF report with survey results as a table."""
    answers = data.get("answers", {})
    pdf = FPDF()
    pdf.add_page()
    pdf.set_auto_page_break(auto=True, margin=15)

    font_path = _find_dejavu_font()
    pdf.add_font("dejavu", "", font_path)
    bold_path = font_path.replace("DejaVuSans.ttf", "DejaVuSans-Bold.ttf")
    if os.path.isfile(bold_path):
        pdf.add_font("dejavu", "B", bold_path)
        has_bold = True
    else:
        has_bold = False

    # Title
    pdf.set_font("dejavu", "B" if has_bold else "", 14)
    pdf.cell(0, 10, "Результаты анкетирования", new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.set_font("dejavu", "", 11)
    pdf.cell(0, 7, "Скрининг болезни Фабри", new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.ln(3)

    # Date
    pdf.set_font("dejavu", "", 9)
    pdf.cell(
        0, 6,
        f"Дата формирования: {datetime.now(timezone.utc).strftime('%d.%m.%Y %H:%M')} UTC",
        new_x="LMARGIN", new_y="NEXT",
    )
    pdf.ln(3)

    # Risk score
    score = data.get("fabry_score")
    score_text = data.get("score_interpretation")
    if score is not None and score_text:
        pdf.set_font("dejavu", "B" if has_bold else "", 11)
        pdf.cell(0, 8, f"Оценка риска Фабри: {score} ({score_text})", new_x="LMARGIN", new_y="NEXT")
        pdf.ln(2)

    # Score breakdown
    score_breakdown = data.get("score_breakdown", [])
    if score_breakdown:
        pdf.set_font("dejavu", "B" if has_bold else "", 10)
        pdf.cell(0, 7, "Сработавший скоринг:", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("dejavu", "", 9)
        for item in score_breakdown:
            pdf.cell(
                0, 6,
                f"  {item['label']}: {item['answer']} (+{item['points']})",
                new_x="LMARGIN", new_y="NEXT",
            )
        pdf.ln(3)

    # Role
    role = answers.get("role")
    if role:
        pdf.set_font("dejavu", "", 10)
        pdf.cell(0, 7, f"Роль: {role}", new_x="LMARGIN", new_y="NEXT")
        pdf.ln(2)

    # Main table by nosology blocks
    col_label_w = 90
    col_value_w = 100
    row_h = 7

    for title, keys in NOSOLOGY_BLOCKS:
        block_rows = []
        for key in keys:
            if key in answers:
                label = QUESTION_LABELS.get(key, key)
                value = str(answers[key])
                block_rows.append((label, value))
        if not block_rows:
            continue

        # Check if we need a page break for the header + at least 1 row
        if pdf.get_y() + row_h * 2 > pdf.h - 20:
            pdf.add_page()

        # Section header
        pdf.set_fill_color(200, 215, 235)
        pdf.set_font("dejavu", "B" if has_bold else "", 10)
        pdf.cell(col_label_w + col_value_w, row_h, f"  {title}", new_x="LMARGIN", new_y="NEXT", fill=True)

        # Rows
        pdf.set_font("dejavu", "", 9)
        for i, (label, value) in enumerate(block_rows):
            if i % 2 == 0:
                pdf.set_fill_color(245, 245, 245)
            else:
                pdf.set_fill_color(255, 255, 255)

            x_start = pdf.get_x()
            y_start = pdf.get_y()

            # Calculate needed height for multi-line value
            pdf.set_xy(x_start + col_label_w, y_start)
            value_lines = pdf.multi_cell(col_value_w, row_h, value, split_only=True)
            needed_h = max(row_h, row_h * len(value_lines))

            if y_start + needed_h > pdf.h - 20:
                pdf.add_page()
                x_start = pdf.get_x()
                y_start = pdf.get_y()

            # Draw label cell
            pdf.set_xy(x_start, y_start)
            pdf.cell(col_label_w, needed_h, f"  {label}", fill=True)

            # Draw value cell
            pdf.set_xy(x_start + col_label_w, y_start)
            pdf.multi_cell(col_value_w, row_h, value, fill=True)

            pdf.set_xy(x_start, y_start + needed_h)

        pdf.ln(2)

    # Early exit reason
    early_exit_reason = data.get("early_exit_reason")
    if early_exit_reason:
        pdf.set_font("dejavu", "", 10)
        pdf.cell(0, 7, f"Досрочное завершение: {early_exit_reason}", new_x="LMARGIN", new_y="NEXT")

    return pdf.output()


def calculate_fabry_score_details(
    answers: dict[str, Any],
) -> tuple[float, list[dict[str, Any]]]:
    score = 0.0
    breakdown: list[dict[str, Any]] = []

    for key, option_scores in FABRY_SCORE_RULES.items():
        answer = answers.get(key)
        if answer is None:
            continue

        points = option_scores.get(str(answer), 0)
        score += points

        if points > 0:
            breakdown.append(
                {
                    "key": key,
                    "label": QUESTION_LABELS.get(key, key),
                    "answer": str(answer),
                    "points": points,
                }
            )

    return score, breakdown


def calculate_fabry_score(answers: dict[str, Any]) -> float:
    return calculate_fabry_score_details(answers)[0]


def _should_recommend_doctor_followup(answers: dict[str, Any]) -> bool:
    return answers.get("relatives_fabry") == "Да"


def get_score_interpretation(score: float) -> str:
    """Get interpretation of Fabry risk score."""
    if score >= 3:
        return "Риск выявлен"
    return "Нет индикаторов риска"


async def finish_survey(message: Message, state: FSMContext) -> None:
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
    # TODO: отправить survey_json в внешний сервис
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
    if GROUP_CHAT_ID and bot and admin_forwarding_enabled and should_forward:
        try:
            await bot.send_message(
                GROUP_CHAT_ID,
                build_group_report("🩺 Новая анкета", user_id, chat_id, data, username=username),
            )
        except TelegramBadRequest as e:
            if "chat not found" in str(e).lower():
                admin_forwarding_enabled = False
                logger.warning(
                    "Group forwarding disabled: chat not found for GROUP_CHAT_ID=%s. "
                    "Проверьте ID группы и убедитесь, что бот добавлен в группу.",
                    GROUP_CHAT_ID,
                )
            else:
                logger.exception("Failed to send data to group chat")
        except Exception:
            logger.exception("Failed to send data to group chat")

    _pdf_data_cache[chat_id] = dict(data)
    await state.clear()


async def finish_with_confirmed_diagnosis(message: Message, state: FSMContext) -> None:
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
    # TODO: отправить survey_json в внешний сервис
    logger.info(
        "Survey result JSON (early exit) for user_id=%s:\n%s",
        user_id,
        json.dumps(survey_json, ensure_ascii=False, indent=2),
    )

    if GROUP_CHAT_ID and bot and admin_forwarding_enabled:
        try:
            await bot.send_message(
                GROUP_CHAT_ID,
                build_group_report(
                    "🩺 Анкета завершена досрочно (подтвержденный диагноз Фабри)",
                    user_id,
                    chat_id,
                    data,
                    username=username,
                ),
            )
        except TelegramBadRequest as e:
            if "chat not found" in str(e).lower():
                admin_forwarding_enabled = False
                logger.warning(
                    "Group forwarding disabled: chat not found for GROUP_CHAT_ID=%s. "
                    "Проверьте ID группы и убедитесь, что бот добавлен в группу.",
                    GROUP_CHAT_ID,
                )
            else:
                logger.exception("Failed to send early-finish data to group chat")
        except Exception:
            logger.exception("Failed to send early-finish data to group chat")

    _pdf_data_cache[chat_id] = dict(data)
    await state.clear()


router = Router()


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
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


@router.callback_query(F.data == "hotline")
async def cb_hotline(callback: CallbackQuery, state: FSMContext) -> None:
    sent = await callback.message.answer(f"Позвоните нам по номеру: {HOTLINE_PHONE}")
    await _track_msg(state, sent.message_id)
    await callback.answer()


@router.callback_query(F.data.startswith("consent|"))
async def cb_consent(callback: CallbackQuery, state: FSMContext) -> None:
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


@router.callback_query(SurveyFSM.waiting_choice, F.data.startswith("ans|"))
async def cb_choice_answer(callback: CallbackQuery, state: FSMContext) -> None:
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


@router.message(SurveyFSM.waiting_choice)
async def wrong_input_in_choice(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    idx = data.get("step_index", 0)
    step = step_by_index(idx)
    opts = step.options(data) if step.options else []
    sent = await message.answer(
        "Пожалуйста, выберите вариант из предложенных кнопок ниже. 👇",
        reply_markup=choice_keyboard(idx, opts).as_markup(),
    )
    await _track_msg(state, message.message_id, sent.message_id)


@router.message(SurveyFSM.waiting_text)
async def text_answer(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    idx = data.get("step_index", 0)
    step = step_by_index(idx)

    # Handle shared contact for the phone step
    if step.key == "phone" and message.contact:
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


@router.message(SurveyFSM.collecting_additional)
async def collect_additional(message: Message, state: FSMContext) -> None:
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


@router.callback_query(SurveyFSM.collecting_additional, F.data.startswith("collect_done|"))
async def cb_collect_done(callback: CallbackQuery, state: FSMContext) -> None:
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


@router.callback_query(F.data == "get_pdf")
async def cb_get_pdf(callback: CallbackQuery) -> None:
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


@router.callback_query()
async def cb_fallback(callback: CallbackQuery) -> None:
    await callback.answer("Эта кнопка больше не актуальна.", show_alert=False)


@router.message()
async def message_fallback(message: Message) -> None:
    await message.answer("Чтобы начать анкетирование, отправьте команду /start")


async def main() -> None:
    global bot
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    telegram_log_handler = TelegramLogHandler(level=logging.WARNING)
    telegram_log_handler.setFormatter(
        logging.Formatter("[Лог %(levelname)s] %(asctime)s\n%(message)s")
    )
    logger.addHandler(telegram_log_handler)

    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    retry_delay = 1.0
    while True:
        try:
            logger.info(
                "Starting bot polling | test_mode=%s | group_chat_id=%s | log_chat_id=%s",
                TEST_MODE,
                GROUP_CHAT_ID,
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
    asyncio.run(main())
