"""A list that writes the system number as XX names every system of that family.

`KVXX-3 KALLVATTENLEDNING, KOPPAR`, `VSXX-22 VÄRMEBÄRARE SEKUNDÄR STÅLRÖR`, and a line of its own: "XX AVSER
SYSTEMBETECKNING". The drawing writes KV02-3-15 and VS02-22-12. Read letter for letter no label opened with KVXX-3,
so no code of the list was ever used, every one was taken for a material, every label was refused as no pipe name
- and a set of sheets measured nothing at all. The comparison is made as the strokes allow: 0 read as O, S as 5.
"""
from types import SimpleNamespace

from vvs_engine.semantics.legend import DrawingLegend, LegendEntry, assign_roles, opens_with


def test_a_label_opens_with_the_code_its_number_stands_in_for():
    assert opens_with("KV02-3-15", "KVXX-3")
    assert opens_with("VS02-22-12", "VSXX-22")
    assert opens_with("KVO2-3-15", "KVXX-3")                  # 0 read as O
    assert opens_with("VVO1-53-16", "VVXX-S3")                # ...and S as 5
    assert opens_with("VVO2-3-/KVO2-3-15", "VVXX-3")          # a pair label opens with its first code


def test_what_follows_the_number_must_agree():
    assert not opens_with("VS02-22-12", "VSXX-2")             # -22 is not -2
    assert not opens_with("KV02-35-15", "KVXX-3")
    assert not opens_with("KV02", "KVXX-3")                   # the code carries more than the system
    assert not opens_with("S3-R8-110", "BXXX")


def _entry(code, description, y):
    return LegendEntry(code=code, description=description, heading="", bbox=(1000.0, y, 1100.0, y + 4.0))


def _label(text, dn, x, y):
    toks = text.split("-")
    return SimpleNamespace(did=f"{text}@{x}", text=text, system_token=toks[0], tokens=toks, dn=dn,
                           dn_source="inline", pattern="A9-9-9", bbox=(x, y, x + 40.0, y + 5.0))


def test_the_list_names_the_pipes_the_sheet_writes_with_its_placeholder():
    legend = DrawingLegend(entries=[
        _entry("KBXX-3", "KÖLDBÄRARE, KOPPAR", 100), _entry("VSXX-22", "VÄRMEBÄRARE SEKUNDÄR STÅLRÖR", 110),
        _entry("KVXX-3", "KALLVATTENLEDNING, KOPPAR", 120), _entry("BV", "BACKVENTIL", 130),
        _entry("XX", "AVSER SYSTEMBETECKNING", 140)])
    labels = [_label("KB02-3-15", 15, 100, 300), _label("KB02-3-35", 35, 200, 300),
              _label("VS02-22-12", 12, 300, 300), _label("VS02-22-18", 18, 400, 300)]
    assign_roles(legend, labels)
    roles = {e.code: e.role for e in legend.entries}
    assert roles["KBXX-3"] == "system" and roles["VSXX-22"] == "system", roles
    assert all(legend.names_a_pipe(d) for d in labels)
    assert legend.role_of_head("VS02-22-12") == "system"


def test_a_placeholder_code_is_a_system_even_when_its_words_name_a_material_and_no_label_uses_it():
    # KBXX-6 KÖLDBÄRARE, ROSTFRITT STÅL: orden låter som ett material, men XX står för systemnumret
    from vvs_engine.rules import using

    def roles(on):
        legend = DrawingLegend(entries=[_entry("KBXX-6", "KÖLDBÄRARE, ROSTFRITT STÅL", 100),
                                        _entry("VSXX-2", "VÄRMEBÄRARE, SEKUNDÄR STÅLRÖR", 110),
                                        _entry("BV", "BACKVENTIL", 120), _entry("BXXX", "GOLVBRUNN, STÅL", 130)])
        with using({"semantics.legend.PLACEHOLDER_IS_SYSTEM": on}):
            assign_roles(legend, [])
        return {e.code: e.role for e in legend.entries}

    on = roles(True)
    assert on["KBXX-6"] == on["VSXX-2"] == "system" and on["BV"] == "component"
    assert on["BXXX"] != "system"                     # en objektmall, där XXX är löpnumret
    assert roles(False)["KBXX-6"] != "system"
