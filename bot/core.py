"""
Shared business logic for Fabry disease screening bot.

This module contains all platform-agnostic code: questionnaire steps,
validators, scoring, report generation, and PDF export. It has NO
dependencies on aiogram or maxapi.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Literal, Optional

from dotenv import load_dotenv
from fpdf import FPDF

# =========================
# Configuration
# =========================

load_dotenv()

HOTLINE_PHONE = os.getenv("HOTLINE_PHONE", "[Phone8]").strip()
CONSENT_DECLINE_PHONE = os.getenv("CONSENT_DECLINE_PHONE", HOTLINE_PHONE).strip()
FABRY_SCORE_WEIGHTS_PATH = os.path.join(
    os.path.dirname(__file__), "fabry_score_weights.json"
)


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
# Utilities
# =========================

def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_spaces(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip())


# =========================
# Validators
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
    t = _normalize_spaces(text)
    if len(t) < 2:
        return False, "Пожалуйста, укажите ваше ФИО."
    if not re.match(r"^[А-Яа-яЁёA-Za-z\s.\-]+$", t):
        return False, "ФИО может содержать только буквы, пробелы, дефисы и точки."
    return True, ""


def validate_phone(text: str, _: dict[str, Any]) -> tuple[bool, str]:
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
# Condition helpers
# =========================

def _for_patient_or_self(data: dict[str, Any], self_text: str, patient_text: str) -> str:
    return patient_text if _is_doctor(data) else self_text


def _is_doctor(data: dict[str, Any]) -> bool:
    answers = data.get("answers", {})
    role_from_answers = _normalize_spaces(str(answers.get("role", "")))
    role_from_state = _normalize_spaces(str(data.get("role", "")))
    role = role_from_answers or role_from_state
    return role.startswith("Врач")


def _is_patient(data: dict[str, Any]) -> bool:
    return not _is_doctor(data)


def _has_no_fabry_diagnosis(data: dict[str, Any]) -> bool:
    answers = data.get("answers", {})
    return answers.get("fabry_confirmed") != "Да"


def _wants_callback(data: dict[str, Any]) -> bool:
    return data.get("callback_pref") == "Да, я жду обратного звонка"


def _wants_sms(data: dict[str, Any]) -> bool:
    return data.get("sms_pref") == "Да, хочу получить рекомендацию в СМС"


def _should_ask_sms_pref(data: dict[str, Any]) -> bool:
    return _is_patient(data) and not _wants_callback(data)


def _should_ask_phone(data: dict[str, Any]) -> bool:
    if _is_doctor(data):
        return True
    return _wants_callback(data) or _wants_sms(data)


def _should_ask_pain_triggers(data: dict[str, Any]) -> bool:
    answers = data.get("answers", {})
    pain = answers.get("pain_hands_feet")
    return pain is not None and pain != "Никогда" and pain != "Нет"


def _should_recommend_doctor_followup(answers: dict[str, Any]) -> bool:
    return answers.get("relatives_fabry") == "Да"


# =========================
# Step text/options functions
# =========================

def _step_text_role(_: dict[str, Any]) -> str:
    return "Кто заполняет анкету?"


def _step_options_role(_: dict[str, Any]) -> list[str]:
    return ["Пациент", "Врач"]


def _step_text_sex(data: dict[str, Any]) -> str:
    return _for_patient_or_self(data, "Укажите, пожалуйста, ваш пол:", "Укажите, пожалуйста, пол пациента:")


def _step_options_sex(_: dict[str, Any]) -> list[str]:
    return ["Мужской", "Женский"]


def _step_text_age(data: dict[str, Any]) -> str:
    return _for_patient_or_self(
        data,
        "Укажите Ваш возраст (числом)\n\nВажно: у мужчин симптомы проявляются раньше и тяжелее, у женщин – вариабельно",
        "Укажите возраст пациента (числом)\n\nВажно: у мужчин симптомы проявляются раньше и тяжелее, у женщин – вариабельно",
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
        "Есть ли у вас кровные родственники с заболеваниями почек, с заболеваниями сердца, перенесшие инсульт в молодом возрасте (до 50 лет)?",
        "Есть ли у вашего пациента кровные родственники с заболеваниями почек, с заболеваниями сердца, перенесшие инсульт в молодом возрасте (до 50 лет)?",
    )


def _step_text_pain(data: dict[str, Any]) -> str:
    return _for_patient_or_self(
        data,
        "Боли в руках и ногах\nВажно: это один из самых ранних признаков\n\nИспытываете ли вы эпизодические или постоянные боли (жжение, покалывание, онемение) в ладонях и стопах?",
        "Неврологические и болевые синдромы (Акропарестезии)\nВажно: это один из самых ранних признаков\n\nИспытывает ли ваш пациент эпизодические или постоянные боли (жжение, покалывание, онемение) в ладонях и стопах?",
    )


def _step_opts_pain(_: dict[str, Any]) -> list[str]:
    return ["Да", "Никогда"]


def _step_text_crises(data: dict[str, Any]) -> str:
    return _for_patient_or_self(
        data,
        "Усиливаются ли эти боли при физической нагрузке, смене погоды, стрессе или после горячей ванны (бывают ли такие болевые приступы)?",
        "Усиливаются ли у пациента эти боли при физической нагрузке, смене погоды, стрессе или после горячей ванны (так называемые «кризы Фабри»)?",
    )


def _step_text_sweating(data: dict[str, Any]) -> str:
    return _for_patient_or_self(
        data,
        "Бывает ли у вас сниженное потоотделение? Например, вы почти не потеете в спортзале или в жару, перегреваетесь?",
        "Бывает ли у вашего пациента сниженное потоотделение (гипогидроз)? Например, пациент почти не потеет в спортзале или в жару, перегревается?",
    )


def _step_opts_sweating(_: dict[str, Any]) -> list[str]:
    return ["Да", "Нет"]


def _step_text_gi(data: dict[str, Any]) -> str:
    return _for_patient_or_self(
        data,
        "Желудочно-кишечный тракт\n\nБеспокоят ли вас вздутие живота, диарея или боли в животе",
        "Желудочно-кишечный тракт\n\nБеспокоят ли пациента вздутие живота, диарея или боли в животе",
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
        "Кожа\n\nЗамечали ли вы у себя небольшие (1-3 мм) темно-красные, почти черные пятна на коже, выступающие над поверхностность кожи, не вызывающие дискомфорта, не исчезающие при надавливании на элемент, особенно в области:",
        "Кожа (Ангиокератомы)\n\nНаблюдаются ли у пациента небольшие (1-3 мм) темно-красные, почти черные, пятна на коже, выступающие над поверхностность кожи, не вызывающие дискомфорта, не исчезающие при надавливании на элемент, особенно в области:",
    )


def _step_opts_skin(_: dict[str, Any]) -> list[str]:
    return [
        "Между пупком и коленями",
        "На бедрах, ягодицах, в паху",
        "На губах и слизистой рта",
        "Нет, не замечал(а)",
    ]


def _step_text_tachy(data: dict[str, Any]) -> str:
    return _for_patient_or_self(
        data,
        "Сердечно-сосудистая система\n\nБывает ли у вас учащенное сердцебиение или перебои в работе сердца вне физической нагрузки?",
        "Сердечно-сосудистая система\n\nБывает ли у пациента учащенное сердцебиение (тахикардия) или перебои в работе сердца вне физической нагрузки?",
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
        "Есть ли у вас одышка при привычных нагрузках (подъем по лестнице), которая не объясняется лишним весом?",
        "Есть ли у пациента одышка при привычных нагрузках (подъем по лестнице), которая не объясняется лишним весом?",
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
        "Знаете ли вы свой уровень белка в моче или креатинин (были выявлены отклонения в анализах мочи или крови)?",
        "Известны ли показатели пациента по белку в моче (протеинурия) или креатинину (были выявлены отклонения в анализах мочи или крови)?",
    )


def _step_opts_proteinuria(_: dict[str, Any]) -> list[str]:
    return ["Да, были отклонения", "Нет, все в норме", "Не проверял(а)"]


def _step_text_hearing(data: dict[str, Any]) -> str:
    return _for_patient_or_self(
        data,
        "Слух и равновесие\n\nЗамечали ли вы снижение слуха или шум в ушах?",
        "Слух и вестибулярный аппарат\n\nНаблюдаются ли у пациента снижение слуха или шум в ушах?",
    )


def _step_opts_hearing(_: dict[str, Any]) -> list[str]:
    return ["Да (с молодости)", "Да (с возрастом)", "Нет"]


def _step_text_dizziness(data: dict[str, Any]) -> str:
    return _for_patient_or_self(
        data,
        "Сопровождаются ли эти симптомы (или бывают ли отдельно) приступами сильного головокружения, ощущения неустойчивости?",
        "Сопровождаются ли эти симптомы у пациента (или бывают ли отдельно) приступами сильного головокружения, ощущения неустойчивости?",
    )


def _step_text_eyes(data: dict[str, Any]) -> str:
    return _for_patient_or_self(
        data,
        "Глаза\n\nГоворили ли вам офтальмологи о наличии специфических изменений роговицы (например, помутнение роговицы) или изменении сосудов глазного дна?",
        "Глаза (Специфический признак)\n\nСообщали ли офтальмологи о наличии у пациента специфических поражений роговицы (так называемая «вихревидная кератопатия» или помутнение роговицы) или изменении сосудов глазного дна?",
    )


def _step_opts_eyes(_: dict[str, Any]) -> list[str]:
    return ["Да, находили", "Нет, не находили", "Не помню", "Не проверял глаза"]


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


def _step_text_city(data: dict[str, Any]) -> str:
    return _for_patient_or_self(data, "Укажите пожалуйста Ваш город", "Укажите, пожалуйста, Ваш город.")


def _step_text_spec(_: dict[str, Any]) -> str:
    return "Укажите пожалуйста Вашу специализацию и должность?"


def _step_text_workplace(_: dict[str, Any]) -> str:
    return "Укажите пожалуйста место работы?"


def _step_text_additional(data: dict[str, Any]) -> str:
    return _for_patient_or_self(
        data,
        "Имеются ли у вас дополнительные сведения, результаты анализов, которые вы хотите указать?\n\nМожно отправить несколько сообщений: текст, фото, документы.\nКогда закончите — нажмите «✅ Продолжить».",
        "Есть ли дополнительные сведения или результаты анализов пациента, которые нужно указать?\n\nМожно отправить несколько сообщений: текст, фото, документы.\nКогда закончите — нажмите «✅ Продолжить».",
    )


def _step_text_callback_pref(data: dict[str, Any]) -> str:
    return _for_patient_or_self(
        data,
        "Хотите ли вы, чтобы специалист перезвонил Вам по результатам анкеты?",
        "Нужно ли, чтобы специалист перезвонил Вам по результатам анкеты?",
    )


def _step_opts_callback_pref(_: dict[str, Any]) -> list[str]:
    return ["Да, я жду обратного звонка", "Нет, звонок не нужен"]


def _step_text_full_name(data: dict[str, Any]) -> str:
    if _is_doctor(data):
        return "Укажите, пожалуйста, ваше, врача, ФИО (Фамилия Имя Отчество)."
    return "Укажите пожалуйста ваше ФИО (Фамилия Имя Отчество):"


def _step_text_phone(data: dict[str, Any]) -> str:
    if _is_doctor(data):
        return "Укажите, пожалуйста, ваш контактный номер телефона.\nПример: +7XXXXXXXXXX\n\nИли нажмите кнопку «Поделиться номером» ниже."
    return "Укажите пожалуйста ваш номер телефона.\nПример: +7XXXXXXXXXX\n\nИли нажмите кнопку «Поделиться номером» ниже."


# =========================
# Steps definition
# =========================

HOTLINE_REMINDERS: dict[int, str] = {
    6: f"📞 Напоминаем: если у вас возникнут вопросы, вы можете в любой момент позвонить на горячую линию: {HOTLINE_PHONE}",
    21: f"📞 Медицинская часть анкеты завершена. Если вы хотите обсудить результаты со специалистом — звоните на горячую линию: {HOTLINE_PHONE}",
}

STEPS: list[Step] = [
    Step(key="role", kind="choice", text=_step_text_role, options=_step_options_role),
    Step(key="sex", kind="choice", text=_step_text_sex, options=_step_options_sex),
    Step(key="age", kind="text", text=_step_text_age, validator=validate_age),
    Step(key="fabry_confirmed", kind="choice", text=_step_text_genetic, options=_opts_yes_no),
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
    ("Генетика и семейный анамнез", ["fabry_confirmed", "relatives_fabry", "relatives_kidney_heart_stroke"]),
    ("Неврология", ["pain_hands_feet", "pain_triggers", "sweating"]),
    ("Желудочно-кишечный тракт", ["gi_after_meals", "early_satiety"]),
    ("Дерматология", ["angiokeratomas"]),
    ("Кардиология", ["tachycardia", "heart_enlargement", "dyspnea", "myocardial_infarction"]),
    ("Нефрология", ["edema", "proteinuria_creatinine", "chronic_kidney_disease"]),
    ("ЛОР и вестибулярные симптомы", ["hearing_tinnitus", "dizziness"]),
    ("Офтальмология", ["eye_sign"]),
    ("Сосудистые события", ["stroke_tia_history"]),
    ("Профиль врача", ["specialization_position", "workplace"]),
    ("Обратная связь и контакты", ["callback_pref", "sms_pref", "full_name", "phone"]),
    ("Дополнительные сведения", ["additional_info"]),
]


# =========================
# Scoring
# =========================

def _load_fabry_score_rules() -> dict[str, dict[str, float]]:
    try:
        with open(FABRY_SCORE_WEIGHTS_PATH, encoding="utf-8") as fh:
            raw_rules = json.load(fh)
    except OSError as exc:
        raise RuntimeError(f"Failed to read score weights file: {FABRY_SCORE_WEIGHTS_PATH}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON in score weights file: {FABRY_SCORE_WEIGHTS_PATH}") from exc

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
                raise RuntimeError("Each score rule must map string answers to numeric weights.")
            normalized_scores[option_value] = float(points)
        rules[question_key] = normalized_scores

    return rules


FABRY_SCORE_RULES = _load_fabry_score_rules()


def calculate_fabry_score_details(answers: dict[str, Any]) -> tuple[float, list[dict[str, Any]]]:
    score = 0.0
    breakdown: list[dict[str, Any]] = []
    for key, option_scores in FABRY_SCORE_RULES.items():
        answer = answers.get(key)
        if answer is None:
            continue
        points = option_scores.get(str(answer), 0)
        score += points
        if points > 0:
            breakdown.append({"key": key, "label": QUESTION_LABELS.get(key, key), "answer": str(answer), "points": points})
    return score, breakdown


def calculate_fabry_score(answers: dict[str, Any]) -> float:
    return calculate_fabry_score_details(answers)[0]


def get_score_interpretation(score: float) -> str:
    if score >= 3:
        return "Риск выявлен"
    return "Нет индикаторов риска"


# =========================
# Flow helpers
# =========================

def next_step_index(start_from: int, data: dict[str, Any]) -> Optional[int]:
    for i in range(start_from, len(STEPS)):
        if STEPS[i].condition(data):
            return i
    return None


def step_by_index(idx: int) -> Step:
    return STEPS[idx]


# =========================
# Report generation
# =========================

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
        lines.append("  " + ", ".join(f"{k}: {v}" for k, v in sorted(by_type.items())))

    return "\n".join(lines)[:3800]


def build_group_report(title: str, user_id: int, chat_id: int, data: dict[str, Any], username: Optional[str] = None) -> str:
    user_display = f"@{username} (ID: {user_id})" if username else str(user_id)
    report = f"{title}\nПользователь: {user_display}\nЧат: {chat_id}\n\n{format_summary(data)}"
    return report[:4000]


def build_survey_result(user_id: int, chat_id: int, username: Optional[str], data: dict[str, Any]) -> dict[str, Any]:
    answers = data.get("answers", {})
    return {
        "timestamp_utc": _utc_iso(),
        "user_id": user_id,
        "chat_id": chat_id,
        "username": username,
        "role": answers.get("role"),
        "early_exit_reason": data.get("early_exit_reason"),
        "fabry_score": data.get("fabry_score"),
        "score_interpretation": data.get("score_interpretation"),
        "score_breakdown": data.get("score_breakdown", []),
        "answers": answers,
        "additional_payload": data.get("additional_payload", []),
    }


# =========================
# PDF generation
# =========================

def _find_dejavu_font() -> str:
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/TTF/DejaVuSans.ttf",
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
        os.path.join(os.path.dirname(__file__), "DejaVuSans.ttf"),
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    raise FileNotFoundError("DejaVuSans.ttf not found. Install fonts-dejavu-core or place the font next to the script.")


def generate_pdf_report(data: dict[str, Any]) -> bytes:
    answers = data.get("answers", {})
    pdf = FPDF()
    pdf.add_page()
    pdf.set_auto_page_break(auto=True, margin=15)

    font_path = _find_dejavu_font()
    pdf.add_font("dejavu", "", font_path)
    bold_path = font_path.replace("DejaVuSans.ttf", "DejaVuSans-Bold.ttf")
    has_bold = os.path.isfile(bold_path)
    if has_bold:
        pdf.add_font("dejavu", "B", bold_path)

    pdf.set_font("dejavu", "B" if has_bold else "", 14)
    pdf.cell(0, 10, "Результаты анкетирования", new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.set_font("dejavu", "", 11)
    pdf.cell(0, 7, "Скрининг болезни Фабри", new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.ln(3)

    pdf.set_font("dejavu", "", 9)
    pdf.cell(0, 6, f"Дата формирования: {datetime.now(timezone.utc).strftime('%d.%m.%Y %H:%M')} UTC", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)

    score = data.get("fabry_score")
    score_text = data.get("score_interpretation")
    if score is not None and score_text:
        pdf.set_font("dejavu", "B" if has_bold else "", 11)
        pdf.cell(0, 8, f"Оценка риска Фабри: {score} ({score_text})", new_x="LMARGIN", new_y="NEXT")
        pdf.ln(2)

    score_breakdown = data.get("score_breakdown", [])
    if score_breakdown:
        pdf.set_font("dejavu", "B" if has_bold else "", 10)
        pdf.cell(0, 7, "Сработавший скоринг:", new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("dejavu", "", 9)
        for item in score_breakdown:
            pdf.cell(0, 6, f"  {item['label']}: {item['answer']} (+{item['points']})", new_x="LMARGIN", new_y="NEXT")
        pdf.ln(3)

    role = answers.get("role")
    if role:
        pdf.set_font("dejavu", "", 10)
        pdf.cell(0, 7, f"Роль: {role}", new_x="LMARGIN", new_y="NEXT")
        pdf.ln(2)

    col_label_w = 90
    col_value_w = 100
    row_h = 7

    for title, keys in NOSOLOGY_BLOCKS:
        block_rows = []
        for key in keys:
            if key in answers:
                block_rows.append((QUESTION_LABELS.get(key, key), str(answers[key])))
        if not block_rows:
            continue

        if pdf.get_y() + row_h * 2 > pdf.h - 20:
            pdf.add_page()

        pdf.set_fill_color(200, 215, 235)
        pdf.set_font("dejavu", "B" if has_bold else "", 10)
        pdf.cell(col_label_w + col_value_w, row_h, f"  {title}", new_x="LMARGIN", new_y="NEXT", fill=True)

        pdf.set_font("dejavu", "", 9)
        for i, (label, value) in enumerate(block_rows):
            pdf.set_fill_color(245, 245, 245) if i % 2 == 0 else pdf.set_fill_color(255, 255, 255)
            x_start = pdf.get_x()
            y_start = pdf.get_y()
            pdf.set_xy(x_start + col_label_w, y_start)
            value_lines = pdf.multi_cell(col_value_w, row_h, value, split_only=True)
            needed_h = max(row_h, row_h * len(value_lines))
            if y_start + needed_h > pdf.h - 20:
                pdf.add_page()
                x_start = pdf.get_x()
                y_start = pdf.get_y()
            pdf.set_xy(x_start, y_start)
            pdf.cell(col_label_w, needed_h, f"  {label}", fill=True)
            pdf.set_xy(x_start + col_label_w, y_start)
            pdf.multi_cell(col_value_w, row_h, value, fill=True)
            pdf.set_xy(x_start, y_start + needed_h)
        pdf.ln(2)

    early_exit_reason = data.get("early_exit_reason")
    if early_exit_reason:
        pdf.set_font("dejavu", "", 10)
        pdf.cell(0, 7, f"Досрочное завершение: {early_exit_reason}", new_x="LMARGIN", new_y="NEXT")

    return pdf.output()
