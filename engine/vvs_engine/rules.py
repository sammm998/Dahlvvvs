"""Varje regel läsningen följer, samlad på ett ställe.

Motorn avgör ingenting med en magisk siffra. Varje gräns i den har ett skäl, och skälet står i koden där gränsen
används. Det hjälper den som läser koden och ingen annan: den som ser en mängd bli fel på skärmen kan inte öppna
`attachment.py` för att förstå varför en hänvisningslinje inte räckte fram.

Det här är samma gränser, beskrivna en gång till för den som läser ritningen i stället för koden - vad regeln
avgör, vad den står på nu, vad den står på från början, och vilken av akademins figurer som visar vad den handlar
om. Registret äger inte värdena; de bor kvar där de används, och ett prov håller registret och koden lika.

En del av dem går att ändra per läsning. Det sker aldrig globalt - jobb körs på trådar i samma process, och ett
värde satt för en ritning skulle annars gälla någon annans - utan genom `using()`, som binder ändringarna till
den tråd som läser just den ritningen.
"""
from __future__ import annotations

import importlib
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Iterator


@dataclass(frozen=True)
class Rule:
    id: str                     # "attachment.NEAR_MISS" - modul och konstant
    group: str
    title: str
    why: str                    # vad regeln avgör, på ritningens språk
    unit: str                   # pt | andel | antal | grader | sekunder | textrader | ""
    default: Any
    lo: Any = None
    hi: Any = None
    figure: str | None = None   # akademifiguren som visar vad regeln handlar om
    tunable: bool = False       # går att ändra per läsning
    fixed_why: str = ""         # ...och om inte: varför

    @property
    def module(self) -> str:
        return self.id.rsplit(".", 1)[0]

    @property
    def const(self) -> str:
        return self.id.rsplit(".", 1)[1]

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "group": self.group, "title": self.title, "why": self.why, "unit": self.unit,
                "default": self.default, "lo": self.lo, "hi": self.hi, "figure": self.figure,
                "tunable": self.tunable, "fixed_why": self.fixed_why, "value": value(self.id, self.default)}


G_LEADER = "Hänvisningslinjer och anslutningar"
G_LEGEND = "Förklaringslistan"
G_PIPE = "Vad som räknas som rör"
G_OWN = "Vem sträckan tillhör"
G_LABEL = "Beteckningar och etiketter"
G_RISER = "Stigare och lodrätt"
G_SCALE = "Skala"
G_TEXT = "Textläsning"
G_LIMIT = "Gränser för arbetet"


RULES: tuple[Rule, ...] = (
    # ---- hänvisningslinjer -------------------------------------------------------------------------------
    Rule("pipes.ownership.SLIVER_RUN", G_LEADER, "För kort för att bära ett namn",
         "En kedja kortare än så är ingen sträcka utan det ett CAD-utdrag lämnar efter sig när två linjer "
         "delas vid en skärning. En sådan stump får inte ta ett namn och bära det vidare - som brygga tar den "
         "namnet över glapp som aldrig var anslutningar.",
         "pt", 0.6, 0.0, 5.0, "pipe", True),
    Rule("pipes.representation.FIGURE_INK", G_LEADER, "När ritat bläck är en figur och inte en sträcka",
         "En rörsträcka går någonstans: bläcket den kostar köper avstånd. En ritad figur - en radiator "
         "skrafferad tät, en pump, en golvbrunn - står still och fyller sin egen ruta om och om igen. En "
         "sammanhängande del vars bläck är så här många gånger sin egen utsträckning är en figur, inte ett rör, "
         "och mäts inte. Mätt på korpusen ligger sträckor på 0,7-3,1 och skrafferade figurer på 8,8-16.",
         "gånger", 6.0, 3.0, 30.0, "pipe", True),
    Rule("pipes.ownership.DECLARED_RUN_MAX_M", G_LEADER, "Så lång får en förklarad kopplingsledning vara",
         "Bladets tabell namnger kopplingsledningar från fördelare till apparat, och en sådan är kort. En "
         "onämnd sträcka längre än så på den förklarade pennan är en stam vars etikett läsningen inte nådde; "
         "regeln för kopplingsledningar namnger den inte, och den står kvar som onämnd.",
         "m", 15.0, 3.0, 60.0, "pipe", True),
    Rule("measure.measure.DOUBLE_LINE_MAX", G_LEADER, "Så tätt ligger ett rörs två kanter",
         "Ett grövre rör ritas som två linjer - sina två kanter - några punkter isär, och en etikett med ett "
         "streck på var kant namnger båda. Två sträckor med samma namn och samma penna som ligger sida vid sida, "
         "så nära som det här, längs större delen av den kortare, är ett rör: den längre kanten bär metrarna. "
         "Två rör med samma namn som bara löper bredvid varandra en bit förblir två.",
         "pt", 8.0, 2.0, 20.0, "pipe", True),
    Rule("semantics.attachment.COLLECTOR_MAX", G_LEADER, "Så lång får en samlingslinje vara",
         "Ritaren drar flera hänvisningslinjer till ett streck och det strecket vidare till röret. Ett sådant "
         "streck på skrivpennan får bära ledaren till röret om det är kort och rakt; en lång linje på "
         "skrivpennan är något annat och får aldrig bli en brygga.",
         "pt", 90.0, 10.0, 300.0, "leader", True),
    Rule("semantics.attachment.CONTACT_TOL", G_LEADER, "Kontakt räknas som kontakt",
         "Hur nära en hänvisningslinjes ände måste ligga rörets linje för att räknas som att den rör vid den. "
         "Under det här är det samma punkt så långt en PDF-export kan uttrycka den.",
         "pt", 0.6, 0.1, 3.0, "leader", True),
    Rule("semantics.attachment.NEAR_MISS", G_LEADER, "Linjen stannar strax intill",
         "Hur långt före sitt rör en hänvisningslinje får stanna och ändå räknas som att den pekar på det. "
         "Vidgningen gäller bara där det inte finns något annat rör inom räckhåll.",
         "pt", 6.0, 0.0, 20.0, "leader", True),
    Rule("semantics.attachment.NEAR_ONE", G_LEADER, "...och att det bara finns ett rör där",
         "Hur nära varandra de rör som ligger inom räckvidden måste ligga för att vara en enda ritad sak. "
         "Ligger de isär har ritningen inte sagt vilket som menas, och sträckan lämnas onämnd.",
         "pt", 2.5, 0.5, 10.0, "bundle", True),
    Rule("semantics.attachment.EDGES_APART_MIN", G_LEADER, "Linjen stannar inne i röret",
         "Ett rör ritat i skala ritas som sina två kanter, och hänvisningslinjen stannar mitt i röret utan att "
         "röra någon av dem. Kanterna måste ligga minst så här långt isär för att vara ett rörs två kanter och "
         "inte ett streck ritat två gånger.",
         "pt", 1.5, 0.5, 5.0, "leader", True),
    Rule("semantics.attachment.EDGES_APART_MAX", G_LEADER, "...och kanterna ligger ett rörs bredd isär",
         "Hur långt isär ett rörs två kanter högst ritas. Längre isär är det två saker och inte ett rör, och "
         "linjen som stannar mellan dem har inte sagt vilken den menar. Kanterna måste dessutom vara samma "
         "ritade streck, runt rörets ände: två rör bredvid varandra är två streck.",
         "pt", 8.0, 2.0, 20.0, "leader", True),
    Rule("semantics.attachment.EDGES_PARALLEL", G_LEADER, "...och går åt samma håll",
         "Hur mycket ett rörs två kanter får skilja i riktning. Ett rör har parallella kanter; går de isär är "
         "det inte ett rör linjen stannat i.",
         "grader", 2.0, 0.0, 10.0, "leader", True),
    Rule("semantics.attachment.DASH_GAP_MAX", G_LEADER, "Landar i en lucka i strecken",
         "Den bredaste ritade luckan i ett streckat rör som en hänvisningslinje får sluta i och ändå räknas som "
         "att den träffat röret.",
         "pt", 8.0, 0.0, 30.0, "leader", True),
    Rule("semantics.attachment.MARKER_MAX", G_LEADER, "Punkter och små ringar vid röränden",
         "Hur stor en sluten markering vid en röraände får vara för att räknas som rörets ände och inte som ett "
         "eget föremål.",
         "pt", 3.0, 0.5, 12.0, None, True),
    Rule("semantics.attachment.SYMBOL_MAX", G_LEADER, "Symboler en linje får peka på",
         "Hur stor en sluten symbol får vara för att en hänvisningslinje ska få peka på den i stället för på "
         "röret självt - stigarmärken, ändcirklar, kopplingar.",
         "pt", 20.0, 2.0, 60.0, "riser", True),
    Rule("semantics.attachment.BUNDLE_SPAN", G_LEADER, "Hur brett ett rörknippe är",
         "Hur långt isär parallella rör får ligga och ändå räknas som ett knippe som en etikettstapel namnger "
         "uppifrån och ned.",
         "pt", 34.0, 5.0, 120.0, "bundle", True),
    Rule("semantics.attachment.COLLINEAR_DEG", G_LEADER, "Samma rör, eller två som möts",
         "Hur mycket två sträckor får luta olika och ändå räknas som samma rör. Slutar en hänvisningslinje i "
         "ett märke mitt på ett stråk rör den samma rör åt två håll; går sträckorna isär är det en koppling "
         "där flera rör möts, och då säger linjen inte vilket av dem etiketten menar.",
         "grader", 6.0, 1.0, 30.0, "leader", True),
    Rule("semantics.attachment.COLLINEAR_OFF", G_LEADER, "...och att de ligger på samma linje",
         "Hur långt isär i sidled två sträckor med samma lutning får ligga och ändå vara samma rör. Två "
         "parallella rör i samma märke ligger bredvid varandra och är två rör, inte ett.",
         "pt", 1.2, 0.2, 6.0, "leader", True),
    Rule("semantics.leaders.TOUCH_TOL", G_LEADER, "Delad ändpunkt",
         "Hur nära två ritade linjers ändar måste ligga för att vara samma punkt. Det här är exportens "
         "sifferprecision, inte en bedömning.",
         "pt", 0.15, 0.01, 1.0, None, False,
         "Talet är PDF-exportens precision. Ändras det tolkas avrundningsfel som ritade avstånd."),
    Rule("semantics.leaders.MAX_SEGMENTS", G_LEADER, "Hur många knäckar en linje får ha",
         "En hänvisningslinje ritas med några få raka segment. Fler än så är inte en hänvisning utan något annat "
         "ritat.",
         "antal", 8, 2, 20, None, True),
    Rule("pipeline.LEADER_MIN_SHARE", G_LEADER, "När en penna är hänvisningspennan",
         "Hur stor andel av bladets etiketter en penna måste dra linjer från för att räknas som bladets "
         "hänvisningspenna.",
         "andel", 0.25, 0.0, 1.0, "layers", True),
    Rule("pipeline.LEADER_INK_SHARE", G_LEADER, "...eller att den nästan bara drar hänvisningar",
         "En penna utan lagernamn är hänvisningspennan när hänvisningslinjerna är den här andelen av allt den "
         "ritar.",
         "andel", 0.5, 0.0, 1.0, "layers", True),
    Rule("pipeline.WRITING_PEN_MIN_STROKES", G_LEADER, "Hur många streck en skrivpenna måste ha",
         "En penna vars varenda streck börjar eller slutar vid en beteckningsruta skriver i stället för att "
         "rita, och tas inte som rör. Den måste ha ritat så här många streck innan \"varenda ett\" säger "
         "något om den.",
         "antal", 8, 2, 50, "layers", True),
    Rule("pipeline.WRITING_PEN_TICKED_LABELS", G_LEADER, "...utom där etiketterna märkt pennan",
         "En penna som så här många av bladets etiketter satt sitt märke på ritar rör, också där varje streck "
         "råkar sluta vid något läsningen tog för text.",
         "antal", 2, 1, 20, "layers", True),
    Rule("pipeline.CLOSE_ON_OWNED_TOL", G_LEADER, "Etiketten står på sin egen sträcka",
         "Hur nära en redan namngiven sträcka en etikettände får sluta för att räknas som att den bekräftar "
         "samma sträcka i stället för att vara olöst.",
         "pt", 8.0, 0.0, 30.0, None, True),

    # ---- förklaringslistan -------------------------------------------------------------------------------
    Rule("semantics.legend.MIN_ENTRIES", G_LEGEND, "Kortaste lista som är en lista",
         "Hur många rader en kolumn av koder måste ha för att räknas som handlingens förklaringslista och inte "
         "som en tabellcell eller en notering.",
         "antal", 6, 3, 30, "vocab", True),
    Rule("semantics.legend.MIN_CODES", G_LEGEND, "...och säga sex olika saker",
         "Hur många olika koder listan måste innehålla. En kolumn som upprepar samma kod är en tabellkolumn, "
         "inte ett ordförråd.",
         "antal", 6, 2, 30, "vocab", True),
    Rule("semantics.legend.USED_MIN", G_LEGEND, "Bladet använder sina egna ord",
         "Hur många av listans koder bladet självt måste skriva ut ute på ritningen för att listan ska räknas "
         "som bladets eget ordförråd.",
         "antal", 2, 0, 20, "vocab", True),
    Rule("semantics.legend.ALIGNED_SHARE", G_LEGEND, "...eller att förklaringarna står i en spalt",
         "Andelen förklaringar som delar vänsterkant. En notering har inte en spalt; en lista har det.",
         "andel", 0.6, 0.0, 1.0, "vocab", True),
    Rule("semantics.legend.COL_TOL", G_LEGEND, "Samma spalt",
         "Hur långt två raders vänsterkanter får skilja sig och ändå räknas som samma kolumn.",
         "pt", 3.0, 0.5, 15.0, "vocab", True),
    Rule("semantics.legend.DESC_GAP_ROWS", G_LEGEND, "Hur långt bort förklaringen får stå",
         "Hur långt till höger om koden dess förklaring får börja, mätt i radhöjder.",
         "textrader", 12.0, 2.0, 40.0, "vocab", True),
    Rule("semantics.legend.MAX_CODE_LEN", G_LEGEND, "Längsta kod",
         "Hur många tecken en kod i listan får ha. Längre än så är det en mening, inte en kod.",
         "antal", 10, 3, 24, "vocab", True),

    # ---- vad som räknas som rör --------------------------------------------------------------------------
    Rule("pipes.representation.OVERLAP_ANG", G_PIPE, "Samma linje, ritad två gånger",
         "Hur många grader två ritade linjer får skilja sig och ändå vara samma linje ritad om igen.",
         "grader", 0.6, 0.0, 5.0, None, True),
    Rule("pipes.representation.OVERLAP_OFF", G_PIPE, "...och hur nära de ligger",
         "Hur nära två parallella linjer måste ligga för att vara samma ritade linje och inte två rör.",
         "pt", 0.30, 0.0, 3.0, None, True),
    Rule("pipes.representation.OVERLAP_MIN", G_PIPE, "Kortaste rest som är ritad",
         "En rest kortare än så är en exports avrundning, inte ritad linje.",
         "pt", 0.15, 0.0, 2.0, None, False,
         "Talet är exportens precision, inte en bedömning av ritningen."),
    Rule("pipes.ownership.SLIVER_ELONGATION", G_PIPE, "Ett tunt föremål, inte ett rör",
         "Hur många gånger längre än brett ett par parallella linjer måste löpa för att vara ett tunt föremål "
         "sett från sidan - en radiator, en bänk - och inte två rör.",
         "andel", 20.0, 2.0, 100.0, None, True),
    Rule("pipes.ownership.SLIVER_PENS", G_PIPE, "...mätt i ritningens egen penna",
         "Hur många pennbredder isär två linjers mitter får ligga och ändå lägga bläck på samma ställe.",
         "andel", 2.0, 0.5, 10.0, None, True),
    Rule("pipes.ownership.SLIVER_COVER", G_PIPE, "Hur mycket paret följs åt",
         "Andelen av längden där de två linjerna verkligen går bredvid varandra.",
         "andel", 0.8, 0.0, 1.0, None, True),
    Rule("pipes.ownership.SLIVER_MIN_REPEATS", G_PIPE, "Ett mellanrum ritningen återkommer till",
         "Hur många gånger samma mellanrum måste återkomma för att vara kontorets sätt att rita ett föremål och "
         "inte två rör som råkade passera nära varandra.",
         "antal", 4, 1, 20, None, True),
    Rule("pipes.ownership.SLIVER_MIN_SHARE", G_PIPE, "...och hur stor del av parem det är",
         "Andelen av familjens nära parallella par som har det mellanrummet.",
         "andel", 0.10, 0.0, 1.0, None, True),
    Rule("pipes.ownership.BOUNDARY_TOL", G_PIPE, "Gräns mellan två sträckor",
         "Hur nära en gränspunkt en primitiv får ligga och räknas som på gränsen.",
         "pt", 0.75, 0.0, 5.0, None, True),
    Rule("pipeline.WRITE_INK_SHARE", G_PIPE, "När en penna skriver i stället för ritar",
         "Läsningen håller bladets skrivpennor utanför andra genomgången, så att en hänvisningslinje aldrig "
         "mäts som rör. Att bära en linje eller ett streck under en beteckning räckte som bevis - och på ett "
         "kontor som ritar rör och etikettramar med samma penna på samma lager försvann därmed ritningen. "
         "Regeln säger hur stor del av pennans eget bläck som måste vara just linjer och ramar för att den ska "
         "räknas som en skrivpenna. Glyferna är redan utanför räkningen: de är text vilketdera det än är.",
         "andel", 0.5, 0.0, 1.0, None, True),
    Rule("pdf.extract.ANNOTATION_INK_IS_REVIEW", G_PIPE, "Påskrift på bladet är inte ritning",
         "En PDF-annotation - ett moln kring en ändring, en notering, eller en mängdning någon redan gjort och "
         "ritat som färgade linjer med längden i kommentaren - ligger ovanpå ritningen och ser i filen ut som "
         "vilket streck som helst. Med regeln på lyfts den av innan bladet läses, och räknas alltså aldrig som "
         "rör; hur mycket som lades åt sidan står kvar i läsningen. Ett blad som inte har någon egen ritning "
         "under påskriften behåller den ändå - där är påskriften det enda som finns att läsa.",
         "", True, None, None, None, True),

    # ---- vem sträckan tillhör ----------------------------------------------------------------------------
    Rule("pipes.ownership.FLOW_LIMIT", G_OWN, "Hur långt en identitet får rinna",
         "En identitet får löpa vidare genom en korsning in i sträckor ritningen inte namnger. Går det flödade "
         "förbi så här många gånger det etiketterna själva avgränsar är det inte längre en läsning av ritningen, "
         "och geometrin blir tvetydig i stället för mätt.",
         "andel", 2.0, 0.5, 10.0, "takeoff", True),
    Rule("pipeline.CLAIM_WALK_LIMIT", G_OWN, "Hur långt ett anspråk följs",
         "Hur många ritade sträckor en olöst etiketts anspråk följs innan det slutar. En linje, inte ett nät.",
         "antal", 600, 20, 5000, None, True),
    Rule("pipeline.SYSTEM_FAMILY_SHARE", G_OWN, "Bladets vana att rita ett system",
         "Hur konsekvent bladet måste lägga ett system på en och samma penna för att vanan ska räknas som "
         "besked om var systemet ritas.",
         "andel", 0.8, 0.0, 1.0, "layers", True),
    Rule("pipeline.SYSTEM_FAMILY_MIN", G_OWN, "...och hur många gånger det sagt det",
         "Hur många gånger bladet måste ha sagt det innan vanan räknas som bevis.",
         "antal", 3, 1, 20, "layers", True),
    Rule("pipeline.PEER_SHARE", G_OWN, "En jämlik penna",
         "En ritad familj som bär den här andelen av den bästa familjens etikettändar är en jämlike till den och "
         "räknas också som rörgeometri.",
         "andel", 0.15, 0.0, 1.0, "layers", True),
    Rule("pipeline.PEER_LABELS_MIN", G_OWN, "...med minst så många egna etiketter",
         "Hur många av bladets egna etiketter som måste peka på familjen för att den ska räknas som jämlike.",
         "antal", 2, 1, 20, "layers", True),

    # ---- beteckningar ------------------------------------------------------------------------------------
    Rule("pipes.ownership.PROPAGATE_TO_STOPS", G_OWN, "Spridning till stopp, grenar ärver",
         "En etikett gäller längs röret tills ny etikett, symbol, dimensionsbyte eller entreprenadgräns, utan "
         "längdbudget; en gren utan egen etikett ärver system och material med osäker dimension "
         "(VS-Kalkyl SPEC 6.4.3 h.3). Av: flödesbudgeten och kravet på bevis i grenens ände gäller.",
         "", True, None, None, None, True),
    Rule("pipes.ownership.CONTRACT_BOUNDARIES", G_OWN, "Entreprenadgräns kapar röret",
         "En not som ENTR. GRÄNS med ledare till ett rör kapar röret där; sidan utan egen etikett är utanför "
         "entreprenaden och räknas för sig, varken bekräftad eller otilldelad. Av: gränsnoter läses inte.",
         "", True, None, None, None, True),
    Rule("semantics.boundary.START_REACH", G_OWN, "Gränsnotens ledare börjar vid texten",
         "Så nära notens textlåda, i texthöjder, måste ett streck börja för att vara notens ledare "
         "(understrykningen ligger en bit under texten).",
         "texthöjder", 0.5, 0.1, 2.0, None, True),
    Rule("semantics.boundary.JOIN_TOL", G_OWN, "Ledarens streck sitter ihop",
         "Två streck vars ändar ligger så nära varandra är en och samma ledare.",
         "pt", 1.0, 0.1, 5.0, None, True),
    Rule("semantics.boundary.MAX_STROKES", G_OWN, "En ledare är några få streck",
         "Växer kedjan från noten till fler streck än så har den vuxit in i ritningen, och noten får ingen punkt.",
         "antal", 6, 1, 50, None, True),
    Rule("semantics.boundary.BOUNDARY_REACH", G_OWN, "Gränsledaren slutar vid röret",
         "Så långt från röret får gränsnotens ledare sluta och ändå peka på det (som en fångad ledarände, h.1).",
         "pt", 6.0, 0.0, 20.0, None, True),
    Rule("pipes.ownership.FLOW_MESH_CYCLES", G_OWN, "När spridd geometri är ett nät",
         "Har det ett namn spridits in i så här många oberoende slingor är det ett nät - väggar, ett rutnät - och "
         "längdbudgeten gäller där. En enda slinga kan vara en ledning som går runt och möter sig själv.",
         "antal", 2, 1, 10, None, True),
    Rule("semantics.attachment.NEAR_MISS_NEAREST", G_LEADER, "Ledarände nära flera rör: ta det närmaste",
         "En ledare som stannat strax före röret fångas av det närmaste röret inom räckhåll (2 × normal "
         "tolerans), om inget annat ligger nästan lika nära (AMBIGUITY_RATIO). Av: då fångas den bara när en "
         "enda sak ligger inom räckhåll.",
         "", True, None, None, None, True),
    Rule("semantics.attachment.NEAR_MISS_CONFIDENCE", G_LEADER, "Konfidens för en fångad ledarände",
         "Ett rör som bara får sitt namn genom en ledare som stannat före det (VS-Kalkyl SPEC 6.4.3 h.1).",
         "andel", 0.7, 0.0, 1.0, None, True),
    Rule("output.artifacts.LEADERS_MEET_MIN", G_LEADER, "Etiketter vars ledare möts utan rör",
         "Så många etiketter vars ledare slutar i samma punkt där inget rör finns rapporteras, med punkten - "
         "en gemensam ledare från ett staplat block eller flera ledare som möts. Där pekar ritningen på något "
         "läsningen inte ser som rör (en stigare, ett schakt).",
         "antal", 3, 2, 20, None, True),
    Rule("semantics.proximity.LEADERLESS_MM", G_LABEL, "Etikett utan ledare: hur nära röret",
         "En rörbeteckning utan ledare tillhör det närmaste rör som löper parallellt med texten, om det ligger "
         "så här nära på papperet (VS-Kalkyl SPEC 6.4.3 h.2).",
         "mm", 6.0, 0.0, 20.0, None, True),
    Rule("semantics.proximity.PARALLEL_DEG", G_LABEL, "Etikett utan ledare: hur parallellt",
         "Så mycket får röret avvika från textens riktning och ändå räknas som det röret etiketten skrivits längs.",
         "grader", 10.0, 0.0, 45.0, None, True),
    Rule("semantics.proximity.AMBIGUITY_RATIO", G_LABEL, "Etikett utan ledare: när två rör är lika nära",
         "Ligger ett annat rör högst så här många gånger så långt bort som det närmaste är tilldelningen "
         "tvetydig och ingen meter bekräftas.",
         "gånger", 1.5, 1.0, 5.0, None, True),
    Rule("semantics.proximity.CONFIDENCE", G_LABEL, "Etikett utan ledare: konfidens",
         "Hur säker en tilldelning utan ledare är (en ledare ger 0,95).",
         "andel", 0.8, 0.0, 1.0, None, True),
    Rule("pipeline.LABELS_MUST_REACH", G_LABEL, "Hur mycket en läsning måste nå",
         "Andelen av bladets rörbeteckningar som måste nå ett rör för att läsningen ska räknas som den rätta av "
         "två möjliga läsningar av bladet.",
         "andel", 0.15, 0.0, 1.0, "leader", True),
    Rule("pipeline.LABELS_MIN", G_LABEL, "...på ett blad med så många etiketter",
         "Under så här många etiketter säger andelen inget, och den används inte.",
         "antal", 20, 1, 200, None, True),
    Rule("semantics.grammar.PATTERN_FLOOR", G_LABEL, "När en form är en form",
         "Hur många ord som måste dela en form för att den ska räknas som bladets sätt att skriva beteckningar "
         "och inte som en engångsföreteelse.",
         "antal", 2.0, 1.0, 20.0, "code", True),
    Rule("routes.ALONGSIDE_BAND", G_LABEL, "Etikett skriven längs sitt rör",
         "Hur långt vid sidan av sin sträcka en etikett får stå, mätt i texthöjder, för att räknas som skriven "
         "längs den.",
         "textrader", 2.2, 0.5, 10.0, None, True),
    Rule("routes.ALONGSIDE_ANGLE", G_LABEL, "...och läsa åt samma håll",
         "Hur många grader etikettens läsriktning får skilja sig från sträckans.",
         "grader", 6.0, 0.0, 45.0, None, True),
    Rule("routes.ALONGSIDE_COVER", G_LABEL, "...och följa den hela vägen",
         "Hur stor del av etikettens egen längd sträckan måste spänna över.",
         "andel", 0.6, 0.0, 1.0, None, True),
    Rule("routes.ALONGSIDE_MIN_LABELS", G_LABEL, "Bladets sätt, inte ett undantag",
         "Hur många etiketter bladet måste skriva så här innan det räknas som bladets sätt att namnge.",
         "antal", 3, 1, 20, None, True),
    Rule("routes.ALONGSIDE_MIN_SHARE", G_LABEL, "...och hur stor del av etiketterna",
         "Andelen av bladets etiketter som skrivs längs sitt rör.",
         "andel", 0.15, 0.0, 1.0, None, True),

    # ---- stigare -----------------------------------------------------------------------------------------
    Rule("pipeline.DN_ROWS_ARE_VERTICAL_ONLY", G_RISER, "Dimension på raden under = bara lodrätt",
         "Står dimensionen på raden under beteckningen namnger etiketten en stigare. Den här regeln avgör om "
         "etiketten DESSUTOM får ge sträckan under sig vågräta meter, eller om den bara räknar en stigare.",
         "", False, None, None, "rows", True),
    Rule("pipeline.TAP_RISER_RINGS", G_RISER, "Liten ring vid rörände = stigare till tappställe",
         "En liten ring (under 4 pt) där ett tunt rör slutar, utan att något rör går igenom den, är stigaren upp "
         "till ett tappställe. Somliga mängdförteckningar räknar de stigarna, andra inte - stäng av regeln där "
         "de inte ska räknas.",
         "", True, None, None, None, True),
    Rule("cli.SET_SCALE_MIN", G_SCALE, "Handlingen är enig om sin skala",
         "Hur många blad som måste ha fastställt samma skala innan omgången räknas som enig och lånar ut den "
         "till ett blad vars egen stämpel inte avgjorde något.",
         "antal", 2, 1, 20, "scale", True),
    Rule("cli.SET_SCALE_TOL", G_SCALE, "...och hur nära de ligger varandra",
         "Hur mycket två blads skalor får skilja sig och ändå räknas som samma skala.",
         "andel", 0.02, 0.0, 0.2, "scale", True),

    # ---- textläsning -------------------------------------------------------------------------------------
    Rule("text.recognize.UNKNOWN_THRESHOLD", G_TEXT, "När ett tecken förblir okänt",
         "Hur säker igenkänningen måste vara på ett tecken. Under det skrivs ett frågetecken i stället för en "
         "gissning - en felläst siffra i en dimension är dyrare än en oläst.",
         "andel", 0.14, 0.0, 1.0, "code", True),
    Rule("text.vector_text.MARK_MAX_PT", G_TEXT, "Hur stort ett bockmärke får vara",
         "Ett ensamt rakt streck bland texten är ett bockmärke - ett pilstreck vid en hänvisningslinjes ände, "
         "ett kryss över den. Större än så är strecket en linje, och linjer hör till geometrin: tas de för "
         "märken försvinner de ur den, och en etikett vars hänvisningslinje försvann pekar inte på något.",
         "pt", 12.0, 2.0, 40.0, "leader", True),
    Rule("text.recognize.MAX_ASPECT", G_TEXT, "Hur avlångt ett tecken får vara",
         "Ett streck som är mycket längre än det är högt är en linje, inte en bokstav.",
         "andel", 12.0, 2.0, 50.0, None, True),
    Rule("text.recognize.ORIENT_LAMBDA", G_TEXT, "Vikt vid streckens riktning",
         "Hur tungt riktningen på ett teckens streck väger mot dess form vid igenkänningen.",
         "andel", 3.0, 0.0, 20.0, None, True),

    # ---- gränser för arbetet -----------------------------------------------------------------------------
    Rule("pipeline.OCR_ASSIST_BUDGET_S", G_LIMIT, "Tid för att namnge enstaka glyfer",
         "Hur länge läsningen får hålla på med att namnge en handfull tecken som inte gick att läsa.",
         "sekunder", 90.0, 0.0, 600.0, None, True),
    Rule("review.agents.OCR_REVIEW_BUDGET_S", G_LIMIT, "Tid för egenkontrollen",
         "En rimlighetskontroll får inte kosta mer än läsningen den kontrollerar.",
         "sekunder", 30.0, 0.0, 300.0, "checks", True),
    Rule("cli.VOCAB_HOLD", G_LIMIT, "Blad som hålls kvar under listsökningen",
         "Hur många av de blad som lästes när handlingens lista söktes som hålls kvar i minnet, i stället för "
         "att läsas om.",
         "antal", 2, 0, 10, None, True),
    Rule("pipeline.DECLINED_SEGMENT_BUDGET", G_LIMIT, "Bortvald geometri som sparas",
         "Hur många ritade streck ur bortvalda familjer en läsning bär med sig, så att de går att titta på "
         "efteråt.",
         "antal", 8000, 0, 100000, None, True),
    Rule("agent.edits.LARGE_CHANGE_M", G_LIMIT, "När en rättelse är stor",
         "En föreslagen ändring större än så räknas som stor och redovisas som sådan.",
         "meter", 50.0, 1.0, 1000.0, None, True),
)


BY_ID = {r.id: r for r in RULES}

_OVERRIDES: ContextVar[dict[str, Any]] = ContextVar("vvs_rule_overrides", default={})


def value(rule_id: str, default: Any) -> Any:
    """What a rule stands at for the reading running on this thread."""
    return _OVERRIDES.get().get(rule_id, default)


@contextmanager
def using(overrides: dict[str, Any] | None) -> Iterator[None]:
    """Bind rule changes to one reading.

    Jobs run on threads in one process. A value set globally would hold for whatever else is being read at the
    same time, and a takeoff would silently be measured under someone else's settings. A context variable is
    bound to the thread that sets it, so a change belongs to the reading that asked for it and to nothing else.
    """
    clean = {k: v for k, v in (overrides or {}).items() if k in BY_ID and BY_ID[k].tunable}
    tok = _OVERRIDES.set({**_OVERRIDES.get(), **clean}) if clean else None
    try:
        yield
    finally:
        if tok is not None:
            _OVERRIDES.reset(tok)


def live_default(r: Rule) -> Any:
    """What the constant actually stands at in the code, so the register can be held against it."""
    mod = importlib.import_module(f".{r.module}", __package__)
    return getattr(mod, r.const)


def catalogue(overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """The whole register, as a reader of the drawing needs it rather than a reader of the code."""
    with using(overrides):
        groups: dict[str, list[dict]] = {}
        for r in RULES:
            groups.setdefault(r.group, []).append(r.as_dict())
        return {"groups": [{"group": g, "rules": rs} for g, rs in groups.items()],
                "n_rules": len(RULES), "n_tunable": sum(1 for r in RULES if r.tunable)}
