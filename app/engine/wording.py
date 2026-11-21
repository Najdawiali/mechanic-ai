"""Putting a reply into the mechanic's language, with every number checked.

Two kinds of text:

  translate()  a text rendered in code from records (a step, a conclusion, a pin list). The model
               only translates it. The result must contain every number, pin and code of the
               English text and nothing more; otherwise it is asked again once, and then the
               English text is sent with a one-line note in the mechanic's language.
  compose()    a reply the model writes from evidence (a case hint, general guidance). The result
               must pass the validator: nothing specific without a source. Asked again once with
               the problems listed; then the caller's fallback (written in code) is used.

Fixed short texts (the "no verified information" reply, notes, labels) are written here in the
three languages and need no model. Arabic and Urdu wording is ours: a native reader should check it.
"""

from __future__ import annotations

import json
import re

from app.engine.model import ModelClient, ModelError, reject
from app.engine.validator import Allowed, problems, tokens
from app.lang.script import Language

LANGUAGE_NAME = {"en": "English", "ar": "Arabic: clear, polite and professional, easily understood in Jordan, in Arabic script, no slang",
                 "ur": "Urdu, written in Urdu script (never Roman Urdu in Latin letters)"}
_ARABIC_SCRIPT = re.compile("[\u0600-\u06ff]")
_LATIN = re.compile("[A-Za-z]")
_SIGNS = "<>≤≥"

FIXED = {
    # The first reply of a session greets the mechanic, once (Ali: polite and professional, not chit-chat).
    "greeting": {
        "en": "Hello, I'm glad to help.",
        "ar": "أهلاً بك، يسعدني مساعدتك.",
        "ur": "السلام علیکم، مدد کر کے خوشی ہوگی۔",
    },
    "english_follows": {
        "en": "",
        "ar": "النص التالي من المصدر بالإنجليزية كما هو:",
        "ur": "درج ذیل متن ماخذ سے انگریزی میں ہے:",
    },
    "no_verified": {
        "en": "Sorry, there is no verified information on this for this car in the knowledge base, so I can't give a pin number, wire colour or value for it. Please take it from the manufacturer's service information or wiring diagram for this car, or ask the workshop's expert.",
        "ar": 'عذراً، لا توجد معلومات موثقة عن هذا لهذه السيارة في قاعدة البيانات، لذلك لا أستطيع إعطاء رقم بن أو لون سلك أو قيمة. يرجى أخذها من معلومات الصيانة أو مخطط الأسلاك الخاص بالشركة المصنعة لهذه السيارة، أو سؤال خبير الورشة.',
        "ur": 'معذرت، اس گاڑی کے لیے نالج بیس میں اس بارے میں کوئی تصدیق شدہ معلومات نہیں ہیں، اس لیے میں پن نمبر، تار کا رنگ یا ویلیو نہیں دے سکتا۔ براہ کرم یہ اس گاڑی کی مینوفیکچرر سروس معلومات یا وائرنگ ڈایاگرام سے لیں، یا ورکشاپ کے ماہر سے پوچھیں۔',
    },
    "general_header": {
        "en": "{codes}: no verified data for this car in the knowledge base. What follows is general knowledge, not verified for this car:",
        "ar": "{codes}: لا توجد بيانات موثقة لهذه السيارة في قاعدة البيانات. ما يلي معلومات عامة غير موثقة لهذه السيارة:",
        "ur": "{codes}: اس گاڑی کے لیے نالج بیس میں کوئی تصدیق شدہ ڈیٹا نہیں ہے۔ درج ذیل عمومی معلومات ہیں، اس گاڑی کے لیے تصدیق شدہ نہیں:",
    },
    "general_header_no_code": {
        "en": "No verified data for this on this car in the knowledge base. What follows is general knowledge, not verified for this car:",
        "ar": "لا توجد بيانات موثقة عن هذا لهذه السيارة في قاعدة البيانات. ما يلي معلومات عامة غير موثقة لهذه السيارة:",
        "ur": "اس گاڑی کے لیے نالج بیس میں اس بارے میں کوئی تصدیق شدہ ڈیٹا نہیں ہے۔ درج ذیل عمومی معلومات ہیں، اس گاڑی کے لیے تصدیق شدہ نہیں:",
    },
    "web_header": {
        "en": "{codes}: no verified data for this car in the knowledge base. What follows comes from web pages, not verified for this car; the links are below:",
        "ar": "{codes}: لا توجد بيانات موثقة لهذه السيارة في قاعدة البيانات. ما يلي مأخوذ من صفحات على الإنترنت وغير موثق لهذه السيارة، والروابط في الأسفل:",
        "ur": "{codes}: اس گاڑی کے لیے نالج بیس میں کوئی تصدیق شدہ ڈیٹا نہیں ہے۔ درج ذیل انٹرنیٹ کے صفحات سے لیا گیا ہے اور اس گاڑی کے لیے تصدیق شدہ نہیں، لنکس نیچے ہیں:",
    },
    "web_check": {
        "en": "Some of these pages may be for another model year or engine: please confirm they match this car before relying on them.",
        "ar": "بعض هذه الصفحات قد تكون لسنة صنع أو محرك آخر، لذا يرجى التأكد من مطابقتها لهذه السيارة قبل الاعتماد عليها.",
        "ur": "ان میں سے کچھ صفحات کسی اور ماڈل سال یا انجن کے ہو سکتے ہیں، اس لیے براہ کرم ان پر بھروسہ کرنے سے پہلے تصدیق کریں کہ یہ اس گاڑی سے میل کھاتے ہیں۔",
    },
    "general_fallback": {
        "en": 'Please start with the basics for this fault: battery voltage, the fuses and grounds of the system, and the connectors of the parts involved. Then tell me what you find.',
        "ar": 'يرجى البدء بالأساسيات لهذا العطل: فولتية البطارية، وفيوزات النظام والأرضي، وكنكتورات القطع المعنية. ثم أخبرني بما تجده.',
        "ur": 'براہ کرم اس خرابی کے لیے بنیادی چیزوں سے شروع کریں: بیٹری وولٹیج، سسٹم کے فیوز اور گراؤنڈ، اور متعلقہ پرزوں کے کنیکٹر۔ پھر مجھے بتائیں کہ کیا ملا۔',
    },
    "fix_noted": {
        "en": 'Thank you. Noted: {fix}. You report the fault as fixed. Please close the session to save this repair for review as a case.',
        "ar": 'شكراً لك. تم تسجيل: {fix}. حسب إفادتك تم حل العطل. يرجى إغلاق الجلسة ليُحفظ هذا الإصلاح كحالة للمراجعة.',
        "ur": 'شکریہ۔ نوٹ کر لیا: {fix}۔ آپ کے مطابق خرابی ٹھیک ہو گئی ہے۔ براہ کرم سیشن بند کریں تاکہ یہ مرمت جائزے کے لیے کیس کے طور پر محفوظ ہو جائے۔',
    },
    "need_reading": {
        "en": 'For this step, please send the meter reading with its unit.',
        "ar": 'لهذه الخطوة، يرجى إرسال قراءة جهاز القياس مع وحدتها.',
        "ur": 'اس مرحلے کے لیے براہ کرم میٹر کی ریڈنگ اس کی یونٹ کے ساتھ بھیجیں۔',
    },
    "which_diagram": {
        "en": 'Which system or component do you need the wiring diagram for?',
        "ar": 'لأي نظام أو قطعة تحتاج مخطط الأسلاك؟',
        "ur": 'آپ کو کس سسٹم یا پرزے کا وائرنگ ڈایاگرام چاہیے؟',
    },
    "diagram_sent": {
        "en": "Here are the wiring diagram pages: {pages}.",
        "ar": "هذه صفحات مخطط الأسلاك: {pages}.",
        "ur": "یہ وائرنگ ڈایاگرام کے صفحات ہیں: {pages}۔",
    },
    "ask_year": {
        "en": 'Which model year is the car? The procedure for {code} is available for: {years}.',
        "ar": 'ما سنة صنع السيارة؟ إجراء الكود {code} متوفر للسنوات: {years}.',
        "ur": 'گاڑی کس ماڈل سال کی ہے؟ کوڈ {code} کا طریقہ ان سالوں کے لیے دستیاب ہے: {years}۔',
    },
    "ask_engine": {
        "en": 'Is the engine {engine}? The procedure for {code} is for that engine only.',
        "ar": 'هل المحرك {engine}؟ إجراء الكود {code} خاص بهذا المحرك فقط.',
        "ur": 'کیا انجن {engine} ہے؟ کوڈ {code} کا طریقہ صرف اسی انجن کے لیے ہے۔',
    },
    "ask_codes": {
        "en": 'What fault code does the scan tool show? If there is none, please describe what the car does.',
        "ar": 'ما كود العطل الظاهر على جهاز الفحص؟ إذا لم يظهر كود، يرجى وصف ما تفعله السيارة.',
        "ur": 'سکینر پر کون سا فالٹ کوڈ آ رہا ہے؟ اگر کوئی کوڈ نہیں ہے تو براہ کرم بتائیں کہ گاڑی میں کیا مسئلہ ہے۔',
    },
    "report_read": {
        "en": 'I read these codes from the report. Please check that they match the screen:',
        "ar": 'قرأت هذه الأكواد من التقرير. يرجى التأكد من مطابقتها لما على الشاشة:',
        "ur": 'میں نے رپورٹ سے یہ کوڈ پڑھے ہیں۔ براہ کرم تصدیق کریں کہ یہ سکرین سے ملتے ہیں:',
    },
    "report_vehicle": {
        "en": "The report names the vehicle as: {vehicle}.",
        "ar": "التقرير يذكر السيارة: {vehicle}.",
        "ur": "رپورٹ میں گاڑی کا نام: {vehicle}۔",
    },
    "report_unreadable": {
        "en": 'Sorry, I could not read any fault code from the attachment. Please type the codes or send a clearer photo.',
        "ar": 'عذراً، لم أتمكن من قراءة أي كود من المرفق. يرجى كتابة الأكواد أو إرسال صورة أوضح.',
        "ur": 'معذرت، میں منسلکہ فائل سے کوئی کوڈ نہیں پڑھ سکا۔ براہ کرم کوڈ لکھ دیں یا زیادہ واضح تصویر بھیجیں۔',
    },
    "which_part": {
        "en": 'Which part do you mean? Please write its name as printed on the wiring diagram, or describe where it is.',
        "ar": 'أي قطعة تقصد؟ يرجى كتابة اسمها كما هو مكتوب على مخطط الأسلاك، أو وصف مكانها.',
        "ur": 'آپ کس پرزے کی بات کر رہے ہیں؟ براہ کرم اس کا نام ویسے لکھیں جیسے وائرنگ ڈایاگرام پر لکھا ہے، یا بتائیں کہ وہ کہاں ہے۔',
    },
    "no_check": {
        "en": "Sorry, I can't offer a check for this wire: {why}",
        "ar": 'عذراً، لا يمكنني اقتراح فحص لهذا السلك: {why}',
        "ur": 'معذرت، میں اس تار کے لیے کوئی چیک نہیں دے سکتا: {why}',
    },
    # -- labels of a step and a conclusion, rendered in code (fifth run: the model translated "Step 1 of 4" to
    #    "الخطوة 1 of 4" in Urdu and left "Normal:" in English). The model translates only the sentences between them.
    "label_step": {"en": "Step {n}:", "ar": "الخطوة {n}:", "ur": "مرحلہ {n}:"},
    "label_step_of": {"en": "Step {n} of {of}:", "ar": "الخطوة {n} من {of}:", "ur": "مرحلہ {n} از {of}:"},
    "label_before": {"en": "Before measuring:", "ar": "قبل القياس:", "ur": "پیمائش سے پہلے:"},
    "label_notice": {"en": "NOTICE:", "ar": "تنبيه:", "ur": "توجہ:"},
    "label_needs_tool": {"en": "This step needs the {tool}.", "ar": "هذه الخطوة تحتاج إلى {tool}.",
                         "ur": "اس مرحلے کے لیے {tool} درکار ہے۔"},
    "label_measure": {"en": "Measure:", "ar": "القياس:", "ur": "پیمائش:"},
    "label_normal": {"en": "Normal:", "ar": "القيمة الطبيعية:", "ur": "درست ریڈنگ:"},
    "label_about": {"en": "about {value}", "ar": "حوالي {value}", "ur": "تقریباً {value}"},
    "label_send_reading": {"en": "Please send me the reading.", "ar": "يرجى إرسال القراءة.", "ur": "براہ کرم مجھے ریڈنگ بھیجیں۔"},
    "label_over_limit": {"en": "OL (over the meter's range)", "ar": "OL (أعلى من مدى جهاز القياس)",
                         "ur": "OL (میٹر کی حد سے زیادہ)"},
    "label_read_as": {"en": "I read your reading as {reading}.", "ar": "فهمت قراءتك على أنها {reading}.",
                      "ur": "میں نے آپ کی ریڈنگ {reading} سمجھی ہے۔"},
    "label_reading": {"en": "Step {n} reading: {reading}, {verdict}", "ar": "قراءة الخطوة {n}: {reading}، {verdict}",
                      "ur": "مرحلہ {n} کی ریڈنگ: {reading}، {verdict}"},
    "label_verdict_normal": {"en": "normal", "ar": "طبيعية", "ur": "درست"},
    "label_verdict_abnormal": {"en": "outside the normal range", "ar": "خارج الحد الطبيعي", "ur": "درست حد سے باہر"},
    "label_result": {"en": "Result:", "ar": "النتيجة:", "ur": "نتیجہ:"},
    "label_all_normal": {"en": "All the steps read normal.", "ar": "كل الخطوات قراءتها طبيعية.",
                         "ur": "تمام مراحل کی ریڈنگ درست ہے۔"},
    "label_confirm": {"en": "After the repair, confirm it:", "ar": "بعد الإصلاح، تأكد منه:", "ur": "مرمت کے بعد اس کی تصدیق کریں:"},
    "label_route": {"en": "Route:", "ar": "المسار:", "ur": "راستہ:"},
    "label_procedure_intro": {
        "en": "This is the manufacturer's troubleshooting procedure for the {vehicle}. "
              "I will give you one step at a time; send me each reading.",
        "ar": "هذا إجراء الفحص من الشركة المصنعة لسيارة {vehicle}. سأعطيك خطوة واحدة في كل مرة؛ أرسل لي كل قراءة.",
        "ur": "یہ {vehicle} کے لیے مینوفیکچرر کا ٹربل شوٹنگ طریقہ ہے۔ میں آپ کو ایک وقت میں ایک مرحلہ دوں گا؛ ہر ریڈنگ مجھے بھیجیں۔",
    },
    "label_circuit_title": {"en": "{part} pin {pin} to {end}", "ar": "{part} pin {pin} إلى {end}",
                            "ur": "{part} pin {pin} سے {end} تک"},
    "label_circuit_intro": {
        "en": "Checks for {title}, worked out from the wiring diagram. The limits are general electrical practice, "
              "not the manufacturer's values. I will give you one step at a time; send me each reading.",
        "ar": "فحوصات {title}، مأخوذة من مخطط الأسلاك. الحدود من الممارسة الكهربائية العامة، وليست قيم الشركة المصنعة. "
              "سأعطيك خطوة واحدة في كل مرة؛ أرسل لي كل قراءة.",
        "ur": "{title} کی جانچ، وائرنگ ڈایاگرام سے تیار کی گئی۔ یہ حدیں عام الیکٹریکل اصول ہیں، مینوفیکچرر کی ویلیوز نہیں۔ "
              "میں آپ کو ایک وقت میں ایک مرحلہ دوں گا؛ ہر ریڈنگ مجھے بھیجیں۔",
    },
    "label_basis": {
        "en": "General electrical practice: a sound wire reads below 1 ohm end to end, and a wire with no short reads above "
              "1 megohm to ground. These are not this manufacturer's published values.",
        "ar": "ممارسة كهربائية عامة: السلك السليم يقرأ أقل من 1 ohm من طرف إلى طرف، والسلك الذي ليس فيه تماس يقرأ أكثر من "
              "1 megohm إلى الأرضي. هذه ليست قيم الشركة المصنعة المنشورة.",
        "ur": "عام الیکٹریکل اصول: درست تار سرے سے سرے تک 1 ohm سے کم پڑھتی ہے، اور جس تار میں شارٹ نہ ہو وہ گراؤنڈ تک "
              "1 megohm سے زیادہ پڑھتی ہے۔ یہ اس مینوفیکچرر کی شائع شدہ ویلیوز نہیں ہیں۔",
    },
    "label_seen_before": {
        "en": "Seen before in the workshop ({car}):", "ar": "حالة سابقة في الورشة ({car}):", "ur": "ورکشاپ میں پہلے دیکھا گیا ({car}):",
    },
    "label_hint_only": {
        "en": "A hint only: follow the steps before replacing any part.",
        "ar": "هذا تلميح فقط: اتبع الخطوات قبل تبديل أي قطعة.",
        "ur": "یہ صرف ایک اشارہ ہے: کوئی پرزہ بدلنے سے پہلے مراحل پر عمل کریں۔",
    },
    # -- wiring answers, written in code (demo, 2026-11-18: translated wiring answers read badly in Arabic) ----
    "wire_pin": {"en": "Pin {pin}", "ar": "البن {pin}", "ur": "پن {pin}"},
    "wire_then": {"en": " then ", "ar": " ثم ", "ur": " پھر "},
    "wire_colour_unknown": {"en": "colour not printed", "ar": "اللون غير مطبوع", "ur": "رنگ درج نہیں"},
    "wire_power": {"en": "power from {end}", "ar": "كهرباء من {end}", "ur": "{end} سے بجلی"},
    "wire_ground": {"en": "ground at {end}", "ar": "أرضي عند {end}", "ur": "{end} پر گراؤنڈ"},
    "wire_to": {"en": "to {end}", "ar": "إلى {end}", "ur": "{end} تک"},
    "wire_to_system": {"en": "to the {end}", "ar": "إلى نظام {end}", "ur": "{end} سسٹم تک"},
    "wire_no_end": {"en": "an end the diagram reader could not follow", "ar": "نهاية لم يتمكن قارئ المخطط من تتبعها",
                    "ur": "ایسا سرا جسے ڈایاگرام ریڈر نہیں پڑھ سکا"},
    "wire_through": {"en": "through {via}", "ar": "عبر {via}", "ur": "{via} کے ذریعے"},
    "wire_drawn_for": {"en": "Drawn for: {versions}.", "ar": "مرسوم لـ: {versions}.", "ur": "اس ورژن کے لیے: {versions}۔"},
    "wire_unfinished": {"en": "The diagram reader could not follow this wire to its end.",
                        "ar": "لم يتمكن قارئ المخطط من تتبع هذا السلك حتى نهايته.",
                        "ur": "ڈایاگرام ریڈر اس تار کو آخر تک نہیں پڑھ سکا۔"},
    "wire_marked": {"en": "Connector {name} is marked \"{marker}\": confirm on the car that it is fitted.",
                    "ar": "الموصل {name} مُعلَّم \"{marker}\": يرجى التأكد من وجوده في السيارة.",
                    "ur": "کنیکٹر {name} پر \"{marker}\" لکھا ہے: گاڑی میں تصدیق کریں کہ یہ لگا ہوا ہے۔"},
    "wire_page": {"en": "page {page}", "ar": "صفحة {page}", "ur": "صفحہ {page}"},
    "wire_page_of": {"en": "{n} of {of}", "ar": "{n} من {of}", "ur": "{n} از {of}"},
    "wire_heading_power": {"en": "Power supply and ground of {component} (wiring diagram: {title}):",
                           "ar": "مصدر الكهرباء والأرضي لـ {component} (مخطط الأسلاك: {title}):",
                           "ur": "{component} کی بجلی اور گراؤنڈ (وائرنگ ڈایاگرام: {title}):"},
    "wire_heading_between": {"en": "{a} to {b} (wiring diagram: {title}):", "ar": "من {a} إلى {b} (مخطط الأسلاك: {title}):",
                             "ur": "{a} سے {b} تک (وائرنگ ڈایاگرام: {title}):"},
    "wire_heading_pins": {"en": "Pins of {component} (wiring diagram: {title}):",
                          "ar": "أطراف {component} (مخطط الأسلاك: {title}):",
                          "ur": "{component} کے پن (وائرنگ ڈایاگرام: {title}):"},
    "wire_if_version": {"en": "If the car is {version}:", "ar": "إذا كانت السيارة {version}:", "ur": "اگر گاڑی {version} ہے:"},
    "wire_every_version": {"en": "On every version:", "ar": "في كل الإصدارات:", "ur": "ہر ورژن میں:"},
    "wire_offer": {"en": "I can send the diagram pages, or guide you step by step to check one of these wires.",
                   "ar": "يمكنني إرسال صفحات المخطط، أو إرشادك خطوة بخطوة لفحص أحد هذه الأسلاك.",
                   "ur": "میں ڈایاگرام کے صفحات بھیج سکتا ہوں، یا ان میں سے کسی تار کی جانچ میں قدم بہ قدم رہنمائی کر سکتا ہوں۔"},
    "wire_which_version": {"en": "Which of these is the car?", "ar": "أي من هذه هي السيارة؟", "ur": "گاڑی ان میں سے کون سی ہے؟"},
    "wire_same_near_end": {
        "en": "The pins and wire colours at the first part are the same on every version; the far end differs by version as "
              "listed. Tell me the version for the route that applies to this car.",
        "ar": "البنات وألوان الأسلاك عند القطعة الأولى هي نفسها في كل الإصدارات؛ الطرف الآخر يختلف حسب الإصدار كما هو مذكور. "
              "أخبرني بإصدار السيارة لأعطيك المسار الخاص بها.",
        "ur": "پہلے پرزے پر پن اور تاروں کے رنگ ہر ورژن میں ایک جیسے ہیں؛ دوسرا سرا ورژن کے مطابق مختلف ہے جیسا کہ لکھا ہے۔ "
              "گاڑی کا ورژن بتائیں تاکہ میں اس کا راستہ دوں۔"},
    "which_wire": {"en": "Which wire do you want to check?", "ar": "أي سلك تريد أن تفحص؟", "ur": "آپ کون سی تار چیک کرنا چاہتے ہیں؟"},
    "choose_part": {"en": "Which part do you mean? Choose one, or write its name as printed on the wiring diagram.",
                    "ar": "أي قطعة تقصد؟ اختر واحدة، أو اكتب اسمها كما هو مكتوب على مخطط الأسلاك.",
                    "ur": "آپ کس پرزے کی بات کر رہے ہیں؟ ایک منتخب کریں، یا اس کا نام ویسے لکھیں جیسے وائرنگ ڈایاگرام پر لکھا ہے۔"},
}


def fixed(name: str, language: Language, /, **values: str) -> str:
    # name and language are positional only, so a text may have its own {name} or {language} slot
    return FIXED[name][language].format(**values)


def _numbers_kept(source: str, result: str) -> bool:
    """Every specific token of the English text is still in the translation, and every comparison sign."""
    have = Allowed.from_texts([result])
    if any(source.count(sign) > result.count(sign) for sign in _SIGNS):
        return False
    return all(have.has(t) for t in tokens(source))


def _in_script(result: str, language: Language) -> bool:
    """An Arabic or Urdu reply is written in Arabic script (part numbers and codes stay in Latin letters).
    First real run: the Urdu reply came back in Roman letters."""
    if language == "en":
        return True
    script, latin = len(_ARABIC_SCRIPT.findall(result)), len(_LATIN.findall(result))
    return script >= 0.25 * (script + latin)


TRANSLATE_SYSTEM = """You translate a message for a car mechanic into {language}. Polite and professional wording.
Keep these exactly as written, in English letters and Latin digits: fault codes, pin numbers, connector and
component names, wire colours, values, units, tool names and menu paths. Every number stays a digit: "Step 1" is
"الخطوة 1", never a word such as "الأولى". Keep the signs < > ≤ ≥ in front of their values. Do not add, drop or explain anything.
Keep the line breaks. Return JSON: {{"message": "..."}}"""


def translate(model: ModelClient, english: str, language: Language, *, role: str = "fast") -> tuple[str, bool]:
    """(text, translated). For English the text is returned as it is."""
    if language == "en" or not english.strip():
        return english, False
    allowed = Allowed.from_texts([english])
    schema = {"type": "object", "additionalProperties": False, "required": ["message"],
              "properties": {"message": {"type": "string"}}}
    user = english
    for _ in range(2):
        try:
            out = model.json("translate", role=role, system=TRANSLATE_SYSTEM.format(language=LANGUAGE_NAME[language]),
                             user=user, schema=schema, max_tokens=1200)
        except ModelError:
            break
        text = out.get("message") if isinstance(out.get("message"), str) else ""
        reasons = ["empty"] if not text.strip() else [
            *([f"no source for {problems(text, allowed)}"] if problems(text, allowed) else []),
            *(["a number, pin, code or comparison sign of the English text is missing"] if not _numbers_kept(english, text) else []),
            *([f"not written in {language} script"] if not _in_script(text, language) else []),
        ]
        if not reasons:
            return text, True
        reject(model, "; ".join(reasons))
        user = (f"{english}\n\n(Your previous translation changed, dropped or added a number, pin, code or comparison sign, "
                f"or was not written in {LANGUAGE_NAME[language]}. Translate again.)")
    lead = fixed("english_follows", language)
    return f"{lead}\n{english}" if lead else english, False


TRANSLATE_LINES_SYSTEM = """You translate short texts for a car mechanic into {language}. Polite and professional wording.
You get a JSON list. Return the same number of items in the same order: each item is the translation of the item at
the same place. Keep these exactly as written, in English letters and Latin digits: fault codes, pin numbers, connector
and component names, wire colours, values, units, tool names and menu paths. Keep the signs < > ≤ ≥ in front of their
values. Do not add, drop, merge or explain anything. Return JSON: {{"lines": ["...", "..."]}}"""


def translate_lines(model: ModelClient, items: list[tuple[str, bool]], language: Language, *,
                    role: str = "fast") -> tuple[list[str], bool]:
    """Translate short texts in one call: (translations in the same order, translated).

    `items` are (English text, required): a required item is a sentence and must come back in the language's script;
    the others (a check name, a preparation such as "IG Mode OFF") may stay as they are. Every item must keep its
    numbers, pins, codes and signs and add none. Asked again once; then the English texts are returned."""
    english = [text for text, _ in items]
    if language == "en" or not english:
        return english, False
    count = len(english)
    schema = {"type": "object", "additionalProperties": False, "required": ["lines"],
              "properties": {"lines": {"type": "array", "items": {"type": "string"}, "minItems": count, "maxItems": count}}}
    system = TRANSLATE_LINES_SYSTEM.format(language=LANGUAGE_NAME[language])
    user = json.dumps({"lines": english}, ensure_ascii=False)
    for _ in range(2):
        try:
            out = model.json("translate", role=role, system=system, user=user, schema=schema, max_tokens=1500)
        except ModelError:
            break
        lines = out.get("lines")
        reasons: list[str] = []
        if not isinstance(lines, list) or len(lines) != count or not all(isinstance(x, str) and x.strip() for x in lines):
            reasons.append(f"not {count} translated items")
        else:
            for (source, required), text in zip(items, lines):
                found = problems(text, Allowed.from_texts([source]))
                if found:
                    reasons.append(f"no source for {found}")
                if not _numbers_kept(source, text):
                    reasons.append("a number, pin, code or comparison sign of the English text is missing")
                if required and not _in_script(text, language):
                    reasons.append(f"not written in {language} script")
        if not reasons:
            return [line.strip() for line in lines], True
        reject(model, "; ".join(dict.fromkeys(reasons)))
        user = json.dumps({"lines": english, "note": f"Your previous answer was refused: {'; '.join(dict.fromkeys(reasons))}. "
                                                     f"Translate again into {LANGUAGE_NAME[language]}."}, ensure_ascii=False)
    return english, False


# The model reports its own length ("120 words.", "120 words limit reached."): fifth run, 5 refused answers.
_BARE_NUMBER = re.compile(r"(?:(?<=[.!?:] )|^)\d+\.(?=\s|$)")  # a sentence that is only a number: "120."
_WORD_COUNT = re.compile(r"(?i)(?:\(\s*)?\b\d+\s+words?(?:\s+(?:limit\s+)?(?:reached|used|max(?:imum)?))?\s*(?:\)|\.)?")


TONE = ("Tone: polite and professional, like an experienced technician advising a colleague. No slang, no jokes, "
        "no small talk and no greeting: the reply adds a greeting itself when one is due.")
# A greeting the model opens with: the reply greets once, in code, in the first reply only (demo, 2026-11-18)
_GREETING = re.compile(r"^\s*(?:(?:مساء|صباح) (?:الخير|النور)|مرحب[اًا]+(?: بك(?:م)?)?|[أا]هل[اًا]+(?: وسهل[اًا]+)?(?: بك(?:م)?)?"
                       r"|السلام عليكم|السلام علیکم|hello|hi|hey|good (?:morning|afternoon|evening|day))(?![A-Za-z])\s*[,.!،:؛۔-]*\s*",
                       re.IGNORECASE)
_QUESTION = re.compile(r"[^.!?؟۔\n]*[?؟]")


def one_question(text: str) -> str:
    """Only the last question of the text is kept (Ali: one question at a time). Earlier questions are removed;
    the statements around them stay as they are."""
    found = list(_QUESTION.finditer(text))
    for match in reversed(found[:-1]):
        text = text[:match.start()] + text[match.end():]
    return re.sub(r"[ \t]{2,}", " ", re.sub(r" +\n", "\n", text)).strip()


def compose(model: ModelClient, purpose: str, *, system: str, facts: dict, language: Language, allowed: Allowed,
            schema_extra: dict | None = None, max_tokens: int = 900, forbidden: re.Pattern[str] | None = None,
            role: str = "reasoning", single_question: bool = False) -> dict | None:
    """The model's reply as a dict with at least "message" (possibly empty), or None when it failed the
    validator twice or did not answer."""
    properties = {"message": {"type": "string"}, **(schema_extra or {})}
    schema = {"type": "object", "additionalProperties": False, "required": list(properties), "properties": properties}
    full_system = (system + f"\n\n{TONE}\nWrite the message in {LANGUAGE_NAME[language]}. Keep fault codes, part and component names, "
                   "tool names and units in English letters, and write every number with Latin digits. "
                   "Never write a number, pin, code, value or specification that is not in the facts or in the mechanic's words.")
    if single_question:
        full_system += "\nAsk exactly one question, at the end of the message."
    user = json.dumps(facts, ensure_ascii=False, default=str)
    for attempt in range(2):
        try:
            out = model.json(purpose, role=role, system=full_system, user=user, schema=schema, max_tokens=max_tokens)
        except ModelError:
            return None
        message = out.get("message") if isinstance(out.get("message"), str) else ""
        message = _BARE_NUMBER.sub("", _WORD_COUNT.sub("", message))  # notes about the prompt's length, not content
        message = re.sub(r"[ \t]{2,}", " ", message.replace(FIXED["greeting"][language], "")).strip()  # greeted in code
        message = _GREETING.sub("", message, count=1).strip()  # its own greeting too ("مساء الخير." after the mechanic's)
        message = message[:1].upper() + message[1:]  # "Good afternoon, check the fuse." -> "Check the fuse."
        asked = len(_QUESTION.findall(message))
        if single_question and asked > 1 and attempt == 1:
            message, asked = one_question(message), 1  # asked again already: keep its last question
        out = {**out, "message": message}
        found = [f"no source for {problems(message, allowed)}"] if problems(message, allowed) else []
        if forbidden is not None:
            found += [f"must not mention {m.group(0)!r}" for m in forbidden.finditer(message)]
        if single_question and asked > 1:
            found.append(f"asks {asked} questions: ask exactly one, at the end")
        if not found:  # an empty message is an answer too ("no record fits"): the caller decides
            return out
        reject(model, "; ".join(found))
        user = json.dumps({**facts, "IMPORTANT": f"Your last message was refused: {'; '.join(found)}. Write it again without these "
                                                 "problems, with no number, pin, code or value that is not in the facts or in "
                                                 "the mechanic's words; do not number a list."}, ensure_ascii=False, default=str)
    return None
