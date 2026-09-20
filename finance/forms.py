from django import forms

from .fund_balance import load_all_funds


class UploadOperationsForm(forms.Form):
    file = forms.FileField(label="Файл операций (.xlsx)")
    replace_existing = forms.BooleanField(
        label="Удалить все текущие загруженные операции перед загрузкой",
        required=False, initial=False,
    )


_INPUT_CLS = "border rounded px-2 py-1 w-full"


class FundTransferForm(forms.Form):
    """Ручное перемещение денег между фондами — см. finance.models.FundTransfer.
    Список фондов берём динамически (из "Фонды" в operation_map.xlsx), а не
    хардкодим — тот же набор, что участвует в расчёте остатка."""

    date = forms.DateField(
        label="Дата", widget=forms.DateInput(attrs={"type": "date", "class": _INPUT_CLS}),
    )
    from_fund = forms.ChoiceField(label="Откуда", widget=forms.Select(attrs={"class": _INPUT_CLS}))
    to_fund = forms.ChoiceField(label="Куда", widget=forms.Select(attrs={"class": _INPUT_CLS}))
    amount = forms.DecimalField(
        label="Сумма", max_digits=14, decimal_places=2, min_value=0.01,
        widget=forms.NumberInput(attrs={"class": _INPUT_CLS, "step": "0.01"}),
    )
    note = forms.CharField(
        label="Комментарий", max_length=255, required=False,
        widget=forms.TextInput(attrs={"class": _INPUT_CLS}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        fund_choices = [(name, name) for name in load_all_funds().keys()]
        self.fields["from_fund"].choices = fund_choices
        self.fields["to_fund"].choices = fund_choices

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("from_fund") and cleaned.get("to_fund") and cleaned["from_fund"] == cleaned["to_fund"]:
            raise forms.ValidationError("Фонд-источник и фонд-назначение должны различаться.")
        return cleaned
