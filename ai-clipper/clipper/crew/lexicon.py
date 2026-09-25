"""Word patterns the specialist agents look for. Each lens reads its own subset; the counts per sentence
are computed once per video (they are evidence, like the footage itself - not anyone's judgment)."""
from __future__ import annotations

import re

from ..analysis import local_judge as lj
from ..editing import safety


def _rx(body: str) -> re.Pattern:
    return re.compile(body, re.I)


PATTERNS: dict[str, re.Pattern] = {
    # ---- content
    "intense": lj.INTENSE,
    "laugh": lj.LAUGH,
    "number": lj.NUMBER,
    "punch": lj.PUNCHLINE,
    "hookpat": _rx(r"\b(the (biggest|worst|best|craziest|only|real) |nobody|no one|here's (the|why|how|what)|"
                   r"what if|imagine|let me tell you|i'll never forget|the truth is|most people)\b"),
    "story": _rx(r"\b(one day|i remember|when i (was|said|told|got|started|quit|moved|met)|years ago|back then|"
                 r"that night|that day|the day|so i|and then|i told|he told|she told|i said|he said|she said|"
                 r"turned around|walked in|showed up|i was like|he goes|she goes|my (boss|dad|mom|friend|coach))\b"),
    "past": _rx(r"\b(was|were|had|did|went|came|told|said|got|saw|knew|thought|\w{3,}ed)\b"),
    "turn": _rx(r"\b(but then|and then|all of a sudden|suddenly|turns out|it turned out|out of nowhere|"
                r"next thing (i|you) know|until|that's when|(days|weeks|months|years|a month|a year) later|"
                r"the next (day|morning|week)|by the time)\b"),
    "lesson": _rx(r"\b(that's why|the lesson|i learned|taught me|since then|ever since|from that day|"
                  r"changed my life|and that's how)\b"),
    "emo_sad": _rx(r"\b(cried|crying|cry|tears|lost (my|him|her)|miss (him|her|them)|passed away|died|grief|"
                   r"lonely|alone|hurt|heartbroken|broke my heart|depressed|depression|funeral)\b"),
    "emo_joy": _rx(r"\b(love|loved|happy|happiest|proud|grateful|blessed|beautiful|best day|so good|thankful)\b"),
    "emo_anger": _rx(r"\b(angry|furious|pissed|hate|hated|mad|rage|disrespect\w*|betray\w*)\b"),
    "emo_fear": _rx(r"\b(scared|afraid|terrified|panic\w*|anxiety|anxious|nervous|fear|freaked out)\b"),
    "vulnerable": _rx(r"\b(i've never told|i never told|never told anyone|to be honest|honestly|i was ashamed|"
                      r"i struggled|my biggest fear|i felt|i feel like|i was broken|i hit rock bottom|"
                      r"i'm not proud)\b"),
    "family": _rx(r"\b(mom|mum|dad|mother|father|son|daughter|brother|sister|wife|husband|grandma|grandpa|"
                  r"my kids?|my family)\b"),
    "funny_cue": _rx(r"\b(joke|jokes|funny|hilarious|kidding|lol|haha|dude|stupid|ridiculous|weird|bro|"
                     r"no way|i'm dead|dying)\b"),
    "teach": _rx(r"\b(how to|the key is|here's how|here's why|the reason (is|why)|step one|first step|the trick|"
                 r"the secret|you need to|you have to|you should|make sure|the way to|what you do is|the rule|"
                 r"the first thing|number one)\b"),
    "explain": _rx(r"\b(because|which means|in other words|for example|for instance|that's because|so that|"
                   r"the reason|basically|essentially)\b"),
    "surprise": _rx(r"\b(actually|turns out|most people (think|don't|believe)|nobody (knows|realizes|talks)|"
                    r"the truth is|believe it or not|surprisingly|the opposite|myth|you'd think|"
                    r"what people don't|little known|here's the crazy part|plot twist)\b"),
    "absolute": _rx(r"\b(always|never|everyone|everybody|nobody|all of them|100 percent|guarantee\w*|the worst|"
                    r"the best|overrated|underrated|trash|garbage|period)\b"),
    "hedge": _rx(r"\b(i think|maybe|perhaps|probably|i guess|kind of|sort of|i'm not sure|might be|could be|"
                 r"in my opinion|i believe|apparently|reportedly|allegedly|i heard)\b"),
    "disagree": _rx(r"\b(disagree|that's not true|you're wrong|no way|that's crazy|come on|are you serious|"
                    r"that's bs|that's bullshit|hold on|wait wait|absolutely not|that's ridiculous)\b"),
    "debate": _rx(r"\b(politic\w*|democrat\w*|republican\w*|trump|biden|abortion|religion|gun control|"
                  r"immigration|feminis\w*|gender|woke|vaccine\w*|capitalism|socialism|cheating|marriage|"
                  r"alpha|red pill|money|dating)\b"),
    "aphorism": _rx(r"\b(the key to|the secret to|if you want|you can't|the only way|the difference between|"
                    r"is not about|it's not about|isn't about|the problem is|the goal is|the point is|"
                    r"you should (never|always)|never (listen|trust|let|give up|stop|quit)|"
                    r"the (biggest|best|worst|hardest) \w+ (is|was|you))\b"),
    "antithesis": _rx(r"\b(not \w+[^.?!]{0,30},? (but|it's)|isn't \w+[^.?!]{0,30},? it's|"
                      r"it's not [^.?!]{1,30},? it's|less \w+[^.?!]{0,20} more)\b"),
    "broad": _rx(r"\b(money|dollars?|million\w*|rich|broke|debt|job|boss|business|salary|work|career|"
                 r"relationship\w*|girlfriend|boyfriend|wife|husband|dating|love|family|kids?|school|college|"
                 r"health|gym|body|food|success|fail\w*|life|friends?|parents?)\b"),
    "specific": _rx(r"\b(thousand|million|billion|hundred|dollars?|months?|years?|weeks?|days?|hours?|"
                    r"minutes?|miles?|pounds?)\b"),
    "first_person": _rx(r"\b(i|i'm|i've|i'd|me|my|myself)\b"),
    "you": _rx(r"\b(you|your|you're|yourself)\b"),
    "question": _rx(r"\?"),
    # ---- promotion / structure
    "promo": lj.PROMO,
    "ad_read": _rx(r"\b(sponsor\w*|brought to you by|promo code|use code|discount|free trial|sign up at|"
                   r"link in (the )?description|dot com slash|athletic greens|ag1|betterhelp|squarespace|"
                   r"manscaped|shopify|hellofresh|nordvpn|expressvpn|draftkings|fanduel|prizepicks)\b"),
    "show_break": lj.SHOW_BREAK,
    "greeting": _rx(r"\b(welcome (back )?to|what's up (guys|everybody|everyone)|hey (guys|everybody|everyone)|"
                    r"thanks for (watching|listening|having me)|see you next|that's (it|all) for|"
                    r"subscribe|hit the bell|like and subscribe)\b"),
    "backref": lj.BACKREF,
    # ---- risk
    "profanity": safety._PATTERN,
    "sexual": _rx(r"\b(sex|sexual\w*|porn\w*|naked|nude\w*|orgasm\w*|hooker\w*|stripper\w*|onlyfans|horny|"
                  r"threesome\w*|blowjob\w*|hooking up)\b"),
    "drugs": _rx(r"\b(cocaine|coke|heroin|meth|weed|marijuana|molly|mdma|lsd|acid trip|shrooms|ketamine|"
                 r"fentanyl|xanax|percs?|got high|snort\w*|overdos\w*|dealer)\b"),
    "violence": _rx(r"\b(kill\w*|murder\w*|shot (him|her|them|at)|shoot\w*|stab\w*|beat (him|her|them) up|"
                    r"punch\w*|blood\w*|gun|knife|bomb\w*|attack\w*|tortur\w*|massacre\w*|choke\w*|strangl\w*)\b"),
    "self_harm": _rx(r"\b(suicid\w*|kill myself|killed himself|killed herself|self[- ]harm|cutting myself|"
                     r"want(ed)? to die|end my life|ending it all)\b"),
    "hate": _rx(r"\b(nigg(a|as|er|ers)|fag|fags|faggot\w*|retard\w*|tranny|kike|spic|chink|"
                r"all (the )?(immigrants|muslims|jews|blacks|whites|gays|mexicans|women|men) (are|should))\b"),
    "weapons": _rx(r"\b(guns?|rifle\w*|pistol\w*|ak-?47|ar-?15|ammo|firearm\w*|glock)\b"),
    "gambling": _rx(r"\b(betting|casino\w*|gambl\w*|sportsbook|parlay\w*|slots|poker)\b"),
    "minor": _rx(r"\b(([1-9]|1[0-7])[- ]years?[- ]old|my (little )?(son|daughter|kid)|little kids?|children|"
                 r"minors?|underage|middle school|high school)\b"),
    "dangerous": _rx(r"\b(challenge where|don't try this|i almost died|jumped off|hold my breath|"
                     r"drove (drunk|high)|drunk driving|no seatbelt)\b"),
    "pii_phone": re.compile(r"(?<!\d)(\+?\d{1,2}[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?!\d)"),
    "pii_email": _rx(r"\b[\w.+-]+@[\w-]+\.[a-z]{2,}\b|\b\w+ at (gmail|yahoo|hotmail|outlook|icloud) dot com\b"),
    "pii_address": _rx(r"\b\d{1,5} [a-z]+( [a-z]+)? (street|avenue|road|boulevard|lane|drive|court|way|place)\b"),
    "pii_secret": _rx(r"\b(social security (number)?|my ssn|credit card number|card number is|my password is|"
                      r"pin number|bank account number|routing number)\b"),
    "claim": _rx(r"(\b\d+(\.\d+)?\s?(%|percent)|\bstudies (show|found)|\bresearch (shows|says|found)|"
                 r"\bscientists (say|found)|\baccording to|\bstatistic\w*|\bproven\b|\bthe data (shows|says)|"
                 r"\bit's a fact|\bthe fact is|\bdoctors (say|won't)|\bcauses? (cancer|autism|disease|heart)|"
                 r"\bcures?\b|\b\d+ times (more|less|higher|lower))"),
    "health": _rx(r"\b(cancer|vaccin\w*|covid|virus|disease|diet|supplement\w*|medic\w*|cure\w*|treatment|"
                  r"doctor\w*|heart attack|diabetes|autism|testosterone|fasting|pills?|dose)\b"),
    "finance": _rx(r"\b(invest\w*|stocks?|crypto\w*|bitcoin|returns?|guaranteed|get rich|passive income|"
                   r"trading|forex|real estate|financial advice)\b"),
    "election": _rx(r"\b(election\w*|ballots?|rigged|voter fraud|stolen election)\b"),
    "misinfo": _rx(r"\b(vaccines? (cause|causes|caused) autism|election was stolen|stolen election|"
                   r"flat earth|5g (causes|caused|spreads)|chemtrails|miracle cure|cures? cancer|"
                   r"big pharma is hiding|plandemic|moon landing (was )?(fake|faked)|crisis actors?|"
                   r"covid (is|was) a hoax|the earth is flat)\b"),
    "conspiracy": _rx(r"\b(they don't want you to know|the elites|deep state|cover[- ]up|"
                      r"mainstream media won't|do your own research|wake up|sheeple|what they're hiding)\b"),
    "accuse": _rx(r"\b(is a (fraud|scammer|criminal|liar|pedophile|predator|rapist|thief|con artist|racist)|"
                  r"(he|she|they) (raped|molested|stole|scammed|abused|assaulted|groomed))\b"),
    "sarcasm": _rx(r"\b(just kidding|i'm kidding|i'm joking|just joking|jk|obviously (i'm )?(kidding|joking)|"
                   r"that was a joke|not really|i'm being sarcastic)\b"),
    "framing": _rx(r"\b(hypothetically|devil's advocate|for the sake of argument|let's say|pretend|"
                   r"imagine if|in theory)\b"),
    "music_tag": _rx(r"\[(music|applause|singing)\]|♪"),
}
# a few features are properties of a whole sentence rather than word counts
BROADCAST = _rx(r"\b(nba|nfl|mlb|nhl|espn|fox news|cnn|msnbc|highlights|official trailer|music video|"
                r"premier league|ufc \d+|full match|full game|live concert)\b")
NAMES = re.compile(r"\b([A-Z][a-z]{2,}(?:\s+[A-Z][a-z]{2,})+)\b")  # multi-word proper names
PRONOUN_START = {"he", "she", "they", "it", "that", "those", "these", "them", "his", "her", "their", "this",
                 "there", "who", "him", "its", "that's", "it's", "they're", "he's", "she's"}
STOP = set("a an the and or but so to of in on at for with from by is was are were be been it this that i you "
           "he she we they me my your his her our their them us do did does have has had not no yes just like "
           "yeah um uh oh okay ok well what when where who how why which there here then than as if about "
           "really very gonna wanna going get got know think mean said say one all out up can would could "
           "should will".split())


def count(name: str, text: str) -> int:
    return len(PATTERNS[name].findall(text))


def hits(name: str, text: str, limit: int = 3) -> list[str]:
    found = []
    for m in PATTERNS[name].finditer(text):
        s = m.group(0).strip()
        if s and s.lower() not in (f.lower() for f in found):
            found.append(s)
        if len(found) >= limit:
            break
    return found
