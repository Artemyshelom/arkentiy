"""
Банковская выписка 1С — парсинг, разбивка по точкам, сверка с iiko.

Формат входа: 1CClientBankExchange v1.03 (Windows-1251), выгрузка из СберБизнес.
Формат выхода: тот же 1С формат, по одному файлу на каждый р/с (точку).
"""

from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_ACCOUNTS_PATH = Path(__file__).resolve().parent.parent.parent / "secrets" / "bank_accounts.json"

_MARKER = "1CClientBankExchange"


# ── dataclasses ──────────────────────────────────────────────────────────────

@dataclass
class BalanceSection:
    account: str
    date_from: str
    date_to: str
    opening: float
    debited: float
    credited: float
    closing: float


@dataclass
class Document:
    doc_type: str          # "Платежное поручение" / "Банковский ордер"
    raw_lines: list[str]   # все строки между СекцияДокумент= и КонецДокумента (включительно)
    fields: dict[str, str] # ключ=значение

    @property
    def payer_account(self) -> str:
        return self.fields.get("ПлательщикРасчСчет", "")

    @property
    def payee_account(self) -> str:
        return self.fields.get("ПолучательРасчСчет", "")

    @property
    def amount(self) -> float:
        try:
            return float(self.fields.get("Сумма", "0"))
        except ValueError:
            return 0.0

    @property
    def date(self) -> str:
        return self.fields.get("Дата", "")

    @property
    def purpose(self) -> str:
        return self.fields.get("НазначениеПлатежа", "")

    @property
    def is_debit(self) -> bool:
        return bool(self.fields.get("ДатаСписано", "").strip())

    @property
    def is_credit(self) -> bool:
        return bool(self.fields.get("ДатаПоступило", "").strip())


@dataclass
class ParsedStatement:
    format_version: str
    encoding: str
    sender: str
    date_from: str
    date_to: str
    accounts: list[str]
    balances: list[BalanceSection]
    documents: list[Document]
    header_lines: list[str]  # raw шапка до первой секции


@dataclass
class BranchResult:
    account: str
    label: str
    city: Optional[str]
    docs_debit: list[Document]   # расходы (мы — плательщик)
    docs_credit: list[Document]  # приходы (мы — получатель)
    balances: list[BalanceSection]
    total_debit: float = 0.0
    total_credit: float = 0.0
    debit_count: int = 0
    credit_count: int = 0


@dataclass
class AcquiringEntry:
    account: str
    doc_date: str
    shift_date: str
    merchant_id: str
    operations: int
    gross_amount: float
    commission: float
    net_amount: float
    # Реквизиты для синтетического документа на комиссию
    our_inn: str = ""        # наш ИНН (из ПолучательИНН исходного doc)
    our_name: str = ""       # наше наименование
    our_kpp: str = ""        # наш КПП
    bank_bik: str = ""       # БИК банка (общий для нас и Сбера)
    bank_korshet: str = ""   # кор/счёт банка
    bank_name: str = ""      # наименование банка
    sbr_account: str = ""    # р/с Сбербанка (из ПлательщикРасчСчет исходного doc)
    sbr_inn: str = ""        # ИНН Сбербанка
    sbr_name: str = ""       # наименование Сбербанка


# ── загрузка конфига ─────────────────────────────────────────────────────────
# Формат bank_accounts.json — по тенантам:
# { "1": { "label": "...", "acquiring_corr_account": "...",
#           "accounts": { "р/с": { "label", "short", "city", "iiko_branch" } } },
#   "3": { ... } }
# Добавить нового тенанта = добавить ключ в JSON. Код менять не нужно.

def load_config(path: Path | None = None) -> dict:
    """Загружает полный конфиг (все тенанты)."""
    p = path or _ACCOUNTS_PATH
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def find_tenant_config(
    config: dict, statement_accounts: set[str],
) -> tuple[str, dict] | None:
    """Определяет тенанта по р/с из выписки. Возвращает (tenant_id, tenant_config)."""
    for tenant_id, tenant in config.items():
        tenant_accs = set(tenant.get("accounts", {}).keys())
        if tenant_accs & statement_accounts:
            return tenant_id, tenant
    return None


def load_accounts_map(path: Path | None = None) -> dict[str, dict]:
    """Плоский маппинг р/с → инфо (все тенанты). Обратная совместимость."""
    config = load_config(path)
    merged: dict[str, dict] = {}
    for tenant in config.values():
        merged.update(tenant.get("accounts", {}))
    return merged


def load_acquiring_corr_account(path: Path | None = None, tenant_id: str = "1") -> str:
    config = load_config(path)
    tenant = config.get(tenant_id, {})
    return tenant.get("acquiring_corr_account", "")


def load_commission_counterpart(path: Path | None = None, tenant_id: str = "1") -> tuple[str, str]:
    """Возвращает (inn, name) кастомного контрагента для документов на комиссию."""
    config = load_config(path)
    tenant = config.get(tenant_id, {})
    return tenant.get("commission_counterpart_inn", ""), tenant.get("commission_counterpart_name", "")


# ── парсер 1С ────────────────────────────────────────────────────────────────

def is_1c_statement(content: str) -> bool:
    return content.lstrip("\ufeff").startswith(_MARKER)


def parse_1c(content: str) -> ParsedStatement:
    content = content.lstrip("\ufeff")
    lines = content.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    header_lines: list[str] = []
    balances: list[BalanceSection] = []
    documents: list[Document] = []
    accounts: list[str] = []

    fmt_ver = ""
    encoding = ""
    sender = ""
    date_from = ""
    date_to = ""

    i = 0
    in_header = True

    while i < len(lines):
        line = lines[i].strip()

        if in_header:
            if line == "СекцияРасчСчет" or line.startswith("СекцияДокумент="):
                in_header = False
            else:
                header_lines.append(lines[i])
                if "=" in line:
                    key, _, val = line.partition("=")
                    if key == "ВерсияФормата":
                        fmt_ver = val
                    elif key == "Кодировка":
                        encoding = val
                    elif key == "Отправитель":
                        sender = val
                    elif key == "ДатаНачала":
                        date_from = val
                    elif key == "ДатаКонца":
                        date_to = val
                    elif key == "РасчСчет":
                        accounts.append(val)
                i += 1
                continue

        # Секция остатков
        if line == "СекцияРасчСчет":
            bal_fields: dict[str, str] = {}
            i += 1
            while i < len(lines) and lines[i].strip() != "КонецРасчСчет":
                bline = lines[i].strip()
                if "=" in bline:
                    k, _, v = bline.partition("=")
                    bal_fields[k] = v
                i += 1
            i += 1  # skip КонецРасчСчет
            balances.append(BalanceSection(
                account=bal_fields.get("РасчСчет", ""),
                date_from=bal_fields.get("ДатаНачала", ""),
                date_to=bal_fields.get("ДатаКонца", ""),
                opening=_float(bal_fields.get("НачальныйОстаток", "0")),
                debited=_float(bal_fields.get("ВсегоСписано", "0")),
                credited=_float(bal_fields.get("ВсегоПоступило", "0")),
                closing=_float(bal_fields.get("КонечныйОстаток", "0")),
            ))
            continue

        # Секция документа
        if line.startswith("СекцияДокумент="):
            doc_type = line.split("=", 1)[1]
            doc_lines = [lines[i]]
            doc_fields: dict[str, str] = {}
            i += 1
            while i < len(lines) and lines[i].strip() != "КонецДокумента":
                dline = lines[i].strip()
                if "=" in dline:
                    k, _, v = dline.partition("=")
                    doc_fields[k] = v
                doc_lines.append(lines[i])
                i += 1
            if i < len(lines):
                doc_lines.append(lines[i])  # КонецДокумента
            i += 1
            documents.append(Document(
                doc_type=doc_type,
                raw_lines=doc_lines,
                fields=doc_fields,
            ))
            continue

        i += 1

    return ParsedStatement(
        format_version=fmt_ver,
        encoding=encoding,
        sender=sender,
        date_from=date_from,
        date_to=date_to,
        accounts=accounts,
        balances=balances,
        documents=documents,
        header_lines=header_lines,
    )


# ── разбивка по точкам ───────────────────────────────────────────────────────

def split_by_branch(
    parsed: ParsedStatement,
    accounts_map: dict[str, dict],
) -> tuple[list[BranchResult], list[Document]]:
    """
    Returns (branch_results, unmatched_docs).
    unmatched_docs — документы, где ни плательщик, ни получатель не в маппинге.
    """
    # Только счета, реально заявленные в этой выписке.
    # Если выписка по Абакану — счета Барнаула сюда не попадут,
    # и переводы между городами не создадут лишних файлов.
    statement_accounts = set(parsed.accounts)
    our_accounts = set(accounts_map.keys()) & statement_accounts

    results: dict[str, BranchResult] = {}
    for acc, info in accounts_map.items():
        if acc not in statement_accounts:
            continue  # счёт не в этой выписке — пропускаем
        acc_balances = [b for b in parsed.balances if b.account == acc]
        results[acc] = BranchResult(
            account=acc,
            label=info["label"],
            city=info.get("city"),
            docs_debit=[],
            docs_credit=[],
            balances=acc_balances,
        )

    unmatched: list[Document] = []

    for doc in parsed.documents:
        payer = doc.payer_account
        payee = doc.payee_account
        matched = False

        if payer in our_accounts:
            results[payer].docs_debit.append(doc)
            results[payer].total_debit += doc.amount
            results[payer].debit_count += 1
            matched = True

        if payee in our_accounts:
            results[payee].docs_credit.append(doc)
            results[payee].total_credit += doc.amount
            results[payee].credit_count += 1
            matched = True

        if not matched:
            unmatched.append(doc)

    return list(results.values()), unmatched


# ── генерация выходного 1С файла ─────────────────────────────────────────────

def generate_1c_file(
    branch: BranchResult,
    parsed: ParsedStatement,
    acquiring: list[AcquiringEntry] | None = None,
    acquiring_corr_account: str = "",
    commission_counterpart_inn: str = "",
    commission_counterpart_name: str = "",
) -> str:
    lines: list[str] = []

    # Суммарная комиссия для корректировки остатков
    total_commission = sum(a.commission for a in acquiring) if acquiring else 0.0

    # Шапка
    lines.append(_MARKER)
    lines.append(f"ВерсияФормата={parsed.format_version}")
    lines.append(f"Кодировка={parsed.encoding}")
    lines.append(f"Отправитель={parsed.sender}")
    lines.append("Получатель=")
    lines.append(f"ДатаСоздания={parsed.date_from}")
    lines.append(f"ВремяСоздания=00:00:00")
    lines.append(f"ДатаНачала={parsed.date_from}")
    lines.append(f"ДатаКонца={parsed.date_to}")
    lines.append(f"РасчСчет={branch.account}")

    # Секции остатков (корректируем на комиссию)
    for bal in branch.balances:
        lines.append("СекцияРасчСчет")
        lines.append(f"ДатаНачала={bal.date_from}")
        lines.append(f"ДатаКонца={bal.date_to}")
        lines.append(f"НачальныйОстаток={_fmt_amount(bal.opening)}")
        lines.append(f"РасчСчет={bal.account}")
        lines.append(f"ВсегоСписано={_fmt_amount(bal.debited + total_commission)}")
        lines.append(f"ВсегоПоступило={_fmt_amount(bal.credited)}")
        lines.append(f"КонечныйОстаток={_fmt_amount(bal.closing - total_commission)}")
        lines.append("КонецРасчСчет")

    # Документы (расходы + приходы, без дублей, в порядке оригинала)
    seen_ids: set[int] = set()
    all_docs = branch.docs_debit + branch.docs_credit
    for doc in all_docs:
        doc_id = id(doc)
        if doc_id in seen_ids:
            continue
        seen_ids.add(doc_id)
        for raw_line in doc.raw_lines:
            lines.append(raw_line)

    # Синтетические расходные документы на комиссию (по одному на каждый день)
    if acquiring:
        for acq in sorted(acquiring, key=lambda a: a.shift_date):
            synth_lines = [
                "СекцияДокумент=Банковский ордер",
                f"Номер=КОМ-{acq.shift_date.replace('.', '')}",
                f"Дата={acq.doc_date}",
                f"Сумма={_fmt_amount(acq.commission)}",
                f"ДатаСписано={acq.doc_date}",
                f"Плательщик={acq.our_name}",
                f"ПлательщикИНН={acq.our_inn}",
                f"ПлательщикКПП={acq.our_kpp}",
                f"ПлательщикРасчСчет={branch.account}",
                f"ПлательщикБанк1={acq.bank_name}",
                f"ПлательщикБИК={acq.bank_bik}",
                f"ПлательщикКорсчет={acq.bank_korshet}",
                f"Получатель={commission_counterpart_name or acq.sbr_name}",
                f"ПолучательИНН={commission_counterpart_inn or acq.sbr_inn}",
                f"ПолучательРасчСчет={'' if commission_counterpart_inn else acq.sbr_account}",
                f"ПолучательБанк1={'' if commission_counterpart_inn else acq.bank_name}",
                f"ПолучательБИК={'' if commission_counterpart_inn else acq.bank_bik}",
                f"ПолучательКорсчет={'' if commission_counterpart_inn else acq.bank_korshet}",
                *(
                    [f"КоррСчет={acquiring_corr_account}"]
                    if acquiring_corr_account else []
                ),
                (
                    f"НазначениеПлатежа=Комиссия по эквайрингу СБЕРБАНК"
                    f" за {acq.shift_date}."
                    f" Мерчант {acq.merchant_id}."
                    f" Операций {acq.operations}."
                ),
                "КонецДокумента",
            ]
            lines.extend(synth_lines)

    lines.append("КонецФайла")
    return "\r\n".join(lines)


# ── парсинг эквайринга ───────────────────────────────────────────────────────

# Сбербанк / СберБизнес: "Возмещение по торговому эквайрингу Мерчант 123 за 08.03.2026. Операций 50. Сумма 100 000 руб., комиссия 1 500 руб."
_ACQ_RE = re.compile(
    r"Возмещение по торговому эквайрингу Мерчант (\d+) за (\d{2}\.\d{2}\.\d{4})\."
    r"\s*Операций (\d+)\.\s*Сумма ([\d\s]+(?:\.\d+)?)\s*руб\.,\s*комиссия ([\d\s]+(?:\.\d+)?)\s*руб\."
)

# Точка банк: "Зачисление средств по операциям. Мерчант №871000265780. Дата реестра 08.03.2026. Комиссия 10 863.42."
# doc.amount = нетто (уже за вычетом комиссии), gross = нетто + комиссия
_ACQ_RE_TOCHKA = re.compile(
    r"Зачисление средств по операциям\.\s*Мерчант №(\d+)\.\s*Дата реестра (\d{2}\.\d{2}\.\d{4})\.\s*Комиссия ([\d\s]+(?:\.\d+)?)[\.\s]"
)


def parse_acquiring(documents: list[Document], accounts_map: dict[str, dict]) -> list[AcquiringEntry]:
    our_accounts = set(accounts_map.keys())
    entries: list[AcquiringEntry] = []

    for doc in documents:
        payee = doc.payee_account
        if payee not in our_accounts:
            continue

        f = doc.fields

        # Формат Сбербанк / СберБизнес
        m = _ACQ_RE.search(doc.purpose)
        if m:
            gross = _parse_spaced_number(m.group(4))
            commission = _parse_spaced_number(m.group(5))
            entries.append(AcquiringEntry(
                account=payee,
                doc_date=doc.date,
                shift_date=m.group(2),
                merchant_id=m.group(1),
                operations=int(m.group(3)),
                gross_amount=gross,
                commission=commission,
                net_amount=round(gross - commission, 2),
                our_inn=f.get("ПолучательИНН", ""),
                our_name=f.get("Получатель", ""),
                our_kpp=f.get("ПолучательКПП", ""),
                bank_bik=f.get("ПлательщикБИК", ""),
                bank_korshet=f.get("ПлательщикКорсчет", ""),
                bank_name=f.get("ПлательщикБанк1", ""),
                sbr_account=f.get("ПлательщикРасчСчет", ""),
                sbr_inn=f.get("ПлательщикИНН", ""),
                sbr_name=f.get("Плательщик", ""),
            ))
            continue

        # Формат Точка банк: gross = нетто (doc.amount) + комиссия из назначения
        m2 = _ACQ_RE_TOCHKA.search(doc.purpose)
        if m2:
            commission = _parse_spaced_number(m2.group(3))
            net = doc.amount
            gross = round(net + commission, 2)
            entries.append(AcquiringEntry(
                account=payee,
                doc_date=doc.date,
                shift_date=m2.group(2),
                merchant_id=m2.group(1),
                operations=0,
                gross_amount=gross,
                commission=commission,
                net_amount=net,
                our_inn=f.get("ПолучательИНН", ""),
                our_name=f.get("Получатель", ""),
                our_kpp=f.get("ПолучательКПП", ""),
                bank_bik=f.get("ПлательщикБИК", ""),
                bank_korshet=f.get("ПлательщикКорсчет", ""),
                bank_name=f.get("ПлательщикБанк1", ""),
                sbr_account=f.get("ПлательщикРасчСчет", ""),
                sbr_inn=f.get("ПлательщикИНН", ""),
                sbr_name=f.get("Плательщик", ""),
            ))
            continue

    return entries


# ── сводка ───────────────────────────────────────────────────────────────────

def build_summary(
    branches: list[BranchResult],
    unmatched: list[Document],
    parsed: ParsedStatement,
    acquiring: list[AcquiringEntry] | None = None,
) -> str:
    total_docs = sum(b.debit_count + b.credit_count for b in branches)
    period = f"{parsed.date_from}—{parsed.date_to}"

    parts: list[str] = []
    parts.append(f"<b>Выписка {period}</b>: {len(parsed.documents)} операций\n")

    for br in branches:
        if br.debit_count == 0 and br.credit_count == 0:
            continue
        acc_short = br.account[-4:]
        parts.append(
            f"<b>{br.label}</b> (р/с ...{acc_short}):\n"
            f"  Приход: {_fmt_rub(br.total_credit)} ({br.credit_count} оп.)\n"
            f"  Расход: {_fmt_rub(br.total_debit)} ({br.debit_count} оп.)"
        )

    if unmatched:
        parts.append(f"\n⚠️ Нераспознанных: {len(unmatched)} оп.")

    # Контроль целостности
    sum_balances_debit = sum(
        sum(b.debited for b in br.balances) for br in branches
    )
    sum_balances_credit = sum(
        sum(b.credited for b in br.balances) for br in branches
    )
    sum_docs_debit = sum(br.total_debit for br in branches)
    sum_docs_credit = sum(br.total_credit for br in branches)

    parts.append(
        f"\n<b>Контроль</b>: "
        f"расход {_fmt_rub(sum_docs_debit)} / приход {_fmt_rub(sum_docs_credit)}"
    )

    # Сверка эквайринга
    if acquiring:
        total_commission = sum(a.commission for a in acquiring)
        parts.append(f"\n<b>Эквайринг Сбер</b>: {len(acquiring)} записей")
        acq_by_branch: dict[str, float] = {}
        comm_by_branch: dict[str, float] = {}
        for a in acquiring:
            acq_by_branch[a.account] = acq_by_branch.get(a.account, 0) + a.gross_amount
            comm_by_branch[a.account] = comm_by_branch.get(a.account, 0) + a.commission
        for acc, gross in sorted(acq_by_branch.items()):
            info = branches_by_account(branches).get(acc)
            label = info.label if info else acc[-4:]
            comm = comm_by_branch.get(acc, 0)
            parts.append(f"  {label}: {_fmt_rub(gross)} (до комиссии), комиссия {_fmt_rub(comm)}")
        parts.append(f"💳 Комиссия вшита в файлы: {_fmt_rub(total_commission)} ({len(acquiring)} документов)")

    return "\n".join(parts)


# ── сверка эквайринга с iiko ──────────────────────────────────────────────────

SBER_ACQ_PAY_TYPES = {"Картой при получении", "Сбербанк"}


def _dd_mm_yyyy_to_iso(s: str) -> str:
    parts = s.strip().split(".")
    if len(parts) == 3:
        return f"{parts[2]}-{parts[1]}-{parts[0]}"
    return s


async def reconcile_acquiring(
    acquiring: list[AcquiringEntry],
    accounts_map: dict[str, dict],
    date_from: str,
    date_to: str,
    statement_accounts: set[str] | None = None,
) -> str | None:
    """
    Сверяет банковский эквайринг Сбер с iiko OLAP v2.
    date_from / date_to в формате DD.MM.YYYY (из 1С выписки).
    Возвращает HTML-строку для Telegram или None если нет данных.
    """
    from app.clients.olap_queries import get_payment_breakdown

    iso_from = _dd_mm_yyyy_to_iso(date_from)
    iso_to_raw = _dd_mm_yyyy_to_iso(date_to)
    iso_to_exclusive = (
        datetime.fromisoformat(iso_to_raw) + timedelta(days=1)
    ).strftime("%Y-%m-%d")

    iiko_data = await get_payment_breakdown(iso_from, iso_to_exclusive)

    bank_by_acc: dict[str, float] = defaultdict(float)
    comm_by_acc: dict[str, float] = defaultdict(float)
    for a in acquiring:
        bank_by_acc[a.account] += a.gross_amount
        comm_by_acc[a.account] += a.commission

    branches_by_city: dict[str, list[dict]] = defaultdict(list)
    for acc, info in accounts_map.items():
        # Показываем только счета, которые есть в загруженной выписке
        if statement_accounts and acc not in statement_accounts:
            continue
        city = info.get("city")
        iiko_branch = info.get("iiko_branch")
        if not city or not iiko_branch:
            continue
        branches_by_city[city].append({
            "acc": acc,
            "label": info["label"],
            "short": info.get("short", info["label"]),
            "iiko_branch": iiko_branch,
        })

    # Порядок городов — по первому появлению в конфиге (стабильный)
    city_order: list[str] = []
    for acc, info in accounts_map.items():
        c = info.get("city")
        if c and c not in city_order:
            city_order.append(c)

    date_display_from = date_from[:5].replace(".", ".")
    date_display_to = date_to[:5].replace(".", ".")
    lines = [f"📊 <b>Сверка эквайринга | {date_display_from}—{date_display_to}</b>"]

    total_bank = 0.0
    total_iiko = 0.0
    total_comm = 0.0
    ok_count = 0
    warn_count = 0

    for city in city_order:
        city_branches = branches_by_city.get(city, [])
        if not city_branches:
            continue

        lines.append(f"\n<b>{city}</b>")

        for br in city_branches:
            bank_gross = bank_by_acc.get(br["acc"], 0)
            commission = comm_by_acc.get(br["acc"], 0)

            iiko_dept = iiko_data.get(br["iiko_branch"], {})
            iiko_acq = sum(
                v for k, v in iiko_dept.items() if k in SBER_ACQ_PAY_TYPES
            )

            diff = round(bank_gross - iiko_acq)
            total_bank += bank_gross
            total_iiko += iiko_acq
            total_comm += commission

            bank_str = f"{bank_gross:,.0f}".replace(",", " ")
            iiko_str = f"{iiko_acq:,.0f}".replace(",", " ")
            comm_str = f"{commission:,.0f}".replace(",", " ")

            if diff == 0:
                emoji = "✅"
                ok_count += 1
            else:
                emoji = "⚠️"
                warn_count += 1

            lines.append(f" {emoji} {br['short']}: {bank_str} → {iiko_str} · комиссия {comm_str}")

            if diff != 0:
                sign = "+" if diff > 0 else ""
                diff_str = f"{diff:,.0f}".replace(",", " ")
                lines.append(f"   <b>△ {sign}{diff_str}</b>")

    total_bank_str = f"{total_bank:,.0f}".replace(",", " ")
    total_iiko_str = f"{total_iiko:,.0f}".replace(",", " ")
    total_comm_str = f"{total_comm:,.0f}".replace(",", " ")
    comm_pct = (total_comm / total_bank * 100) if total_bank else 0

    lines.append(f"\n<b>Итого</b>: банк {total_bank_str} → iiko {total_iiko_str}")
    lines.append(f"💰 Комиссия: {total_comm_str} ({comm_pct:.1f}%)")
    lines.append(f"✅ {ok_count} · ⚠️ {warn_count}")

    if total_bank == 0 and total_iiko == 0:
        return None

    return "\n".join(lines)


# ── вспомогательные ──────────────────────────────────────────────────────────

def branches_by_account(branches: list[BranchResult]) -> dict[str, BranchResult]:
    return {b.account: b for b in branches}


def _float(s: str) -> float:
    try:
        return float(s.replace(" ", ""))
    except (ValueError, TypeError):
        return 0.0


def _parse_spaced_number(s: str) -> float:
    return float(s.replace(" ", "").replace("\xa0", ""))


def _fmt_amount(v: float) -> str:
    if v == int(v):
        return str(int(v))
    return f"{v:.2f}"


def _fmt_rub(v: float) -> str:
    return f"{v:,.0f} \u20bd".replace(",", " ")


# ── основная функция обработки ───────────────────────────────────────────────

def process_statement(content: str, accounts_path: Path | None = None) -> dict:
    """
    Полный цикл: парсинг → разбивка → генерация файлов → сводка.
    Автоматически определяет тенанта по р/с из выписки.
    Возвращает dict с ключами:
      - files, summary, branches, acquiring, unmatched, parsed
      - accounts_map: dict  — маппинг р/с тенанта (для reconcile)
      - tenant_id: str
    """
    config = load_config(accounts_path)
    parsed = parse_1c(content)

    match = find_tenant_config(config, set(parsed.accounts))
    if not match:
        raise ValueError(f"Не найден тенант для р/с: {', '.join(parsed.accounts)}")

    tenant_id, tenant_config = match
    accounts_map = tenant_config["accounts"]
    acq_corr_account = tenant_config.get("acquiring_corr_account", "")
    comm_inn = tenant_config.get("commission_counterpart_inn", "")
    comm_name = tenant_config.get("commission_counterpart_name", "")
    parsed = parse_1c(content)

    logger.info(
        f"[bank_statement] tenant={tenant_id}: {len(parsed.documents)} docs, "
        f"{len(parsed.balances)} balance sections, "
        f"{len(parsed.accounts)} accounts, "
        f"period {parsed.date_from}—{parsed.date_to}"
    )

    branches, unmatched = split_by_branch(parsed, accounts_map)
    acquiring = parse_acquiring(parsed.documents, accounts_map)

    files: dict[str, bytes] = {}
    for br in branches:
        if br.debit_count == 0 and br.credit_count == 0:
            continue
        branch_acq = [a for a in acquiring if a.account == br.account]
        file_content = generate_1c_file(br, parsed, acquiring=branch_acq or None, acquiring_corr_account=acq_corr_account, commission_counterpart_inn=comm_inn, commission_counterpart_name=comm_name)
        period = f"{parsed.date_from}-{parsed.date_to}".replace(".", "")
        filename = f"{br.label}_{period}.txt"
        files[filename] = file_content.encode("windows-1251")

    summary = build_summary(branches, unmatched, parsed, acquiring)

    return {
        "files": files,
        "summary": summary,
        "branches": branches,
        "acquiring": acquiring,
        "unmatched": unmatched,
        "parsed": parsed,
        "accounts_map": accounts_map,
        "tenant_id": tenant_id,
    }
