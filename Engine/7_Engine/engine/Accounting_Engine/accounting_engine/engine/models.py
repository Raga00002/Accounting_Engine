"""Pydantic data models shared by extractor, validator, UI and exporter."""
from __future__ import annotations

from datetime import date
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

TxnType = Literal["sale", "purchase"]
SourceType = Literal["image", "voice", "text", "manual", "import"]


class LineItem(BaseModel):
    item: str = Field(..., description="Item description as written, e.g. 'Rice (BPT)', 'Mustard Oil'")
    qty: Optional[float] = Field(None, description="Count of packages (bags, packets, tins...)")
    unit: Optional[str] = Field(None, description="Package unit: Bag, Pkt, Tin, Kg, Ltr, Qtl ...")
    weight_qtl: Optional[float] = Field(
        None, description="Net weight in quintals when the bill prices by weight (e.g. .52 x 5550)"
    )
    rate: Optional[float] = Field(None, description="Rate per unit or per quintal, as written")
    amount: Optional[float] = Field(None, description="Line amount as written, if written")
    struck_out: bool = Field(False, description="True if the line is crossed out on the bill")
    note: Optional[str] = None

    @field_validator("item")
    @classmethod
    def _strip_item(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("item name cannot be empty")
        return v

    @property
    def computed_amount(self) -> Optional[float]:
        """Amount implied by the numbers on the line (weight x rate preferred, else qty x rate)."""
        if self.weight_qtl is not None and self.rate is not None:
            return round(self.weight_qtl * self.rate, 2)
        if self.qty is not None and self.rate is not None:
            return round(self.qty * self.rate, 2)
        return None

    @property
    def effective_amount(self) -> Optional[float]:
        return self.amount if self.amount is not None else self.computed_amount


class BillDraft(BaseModel):
    """What the extractor produces and the user reviews before saving."""

    party_name_raw: Optional[str] = Field(None, description="Party name exactly as written/spoken")
    bill_date: Optional[date] = None
    bill_no: Optional[str] = None
    txn_type: TxnType = "sale"
    items: list[LineItem] = Field(default_factory=list)
    total_written: Optional[float] = Field(None, description="Grand total as written on the bill, if any")
    source: SourceType = "manual"
    raw_text: Optional[str] = Field(None, description="Transcript (voice) or verbatim transcription (image)")
    notes: list[str] = Field(default_factory=list, description="Extractor remarks / uncertainties")

    @property
    def active_items(self) -> list[LineItem]:
        return [i for i in self.items if not i.struck_out]

    @property
    def computed_total(self) -> Optional[float]:
        amounts = [i.effective_amount for i in self.active_items]
        if not amounts or any(a is None for a in amounts):
            return None
        return round(sum(amounts), 2)


class Party(BaseModel):
    id: Optional[int] = None
    name: str
    group_name: str = "Sundry Debtors"
    opening_balance: Optional[float] = None
    opening_drcr: Optional[Literal["Dr", "Cr"]] = None
    address: Optional[str] = None  # multi-line allowed; exporter splits on newlines
    state: Optional[str] = None
    country: Optional[str] = None
    pincode: Optional[str] = None
    aliases: list[str] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("party name cannot be empty")
        return v


class Bill(BaseModel):
    id: Optional[int] = None
    party_id: int
    bill_date: date
    bill_no: str
    amount: float
    drcr: Literal["Dr", "Cr"]
    txn_type: TxnType = "sale"
    source: SourceType = "manual"
    raw_text: Optional[str] = None
    image_path: Optional[str] = None
    total_written: Optional[float] = None
    exported: bool = False
    items: list[LineItem] = Field(default_factory=list)


class MatchCandidate(BaseModel):
    party: Party
    score: float
    matched_on: str  # the name/alias that produced the score


class ValidationReport(BaseModel):
    missing: list[str] = Field(default_factory=list)  # blocking: must be resolved before save
    warnings: list[str] = Field(default_factory=list)  # non-blocking
    computed_total: Optional[float] = None
    total_written: Optional[float] = None

    @property
    def ok(self) -> bool:
        return not self.missing
