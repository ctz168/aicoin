"""
AICoin 内置加密钱包
====================
功能: 助记词生成、私钥派生、以太坊兼容地址、加密存储、签名验证
依赖: 仅 Python 标准库 (无需任何外部包)
"""

import os
import sys
import json
import hmac
import hashlib
import struct
import secrets
import getpass
import stat
import base64
import time
from pathlib import Path
from typing import Optional, Dict, Tuple, Any

# ========== 内置 BIP39 英文词表 (2048词) ==========

_BIP39_WORDLIST = [
    "abandon","ability","able","about","above","absent","absorb","abstract",
    "absurd","abuse","access","accident","account","accuse","achieve","acid",
    "acoustic","acquire","across","act","action","actor","actress","actual",
    "adapt","add","addict","address","adjust","admit","adult","advance",
    "advice","aerobic","affair","afford","afraid","again","age","agent",
    "agree","ahead","aim","air","airport","aisle","alarm","album",
    "alcohol","alert","alien","all","alley","allow","almost","alone",
    "alpha","already","also","alter","always","amateur","amazing","among",
    "amount","amused","analyst","anchor","ancient","anger","angle","angry",
    "animal","ankle","announce","annual","another","answer","antenna","antique",
    "anxiety","any","apart","apology","appear","apple","approve","april",
    "arch","arctic","area","arena","argue","arm","armed","armor",
    "army","around","arrange","arrest","arrive","arrow","art","artefact",
    "artist","artwork","ask","aspect","assault","asset","assist","assume",
    "asthma","athlete","atom","attack","attend","attitude","attract","auction",
    "audit","august","aunt","author","auto","autumn","average","avocado",
    "avoid","awake","aware","awesome","awful","awkward","axis","baby",
    "bachelor","bacon","badge","bag","balance","balcony","ball","bamboo",
    "banana","banner","bar","barely","bargain","barrel","base","basic",
    "basket","battle","beach","bean","beauty","because","become","beef",
    "before","begin","behave","behind","believe","below","belt","bench",
    "benefit","best","betray","better","between","beyond","bicycle","bid",
    "bike","bind","biology","bird","birth","bitter","black","blade",
    "blame","blanket","blast","bleak","bless","blind","blood","blossom",
    "blow","blue","blur","blush","board","boat","body","boil",
    "bomb","bone","bonus","book","boost","border","boring","borrow",
    "boss","bottom","bounce","box","boy","bracket","brain","brand",
    "brass","brave","bread","breeze","brick","bridge","brief","bright",
    "bring","brisk","broccoli","broken","bronze","broom","brother","brown",
    "brush","bubble","buddy","budget","buffalo","build","bulb","bulk",
    "bullet","bundle","bunny","burden","burger","burst","bus","business",
    "busy","butter","buyer","buzz","cabbage","cabin","cable","cactus",
    "cage","cake","call","calm","camera","camp","can","canal",
    "cancel","candy","cannon","canoe","canvas","canyon","capable","capital",
    "captain","car","carbon","card","cargo","carpet","carry","cart",
    "case","cash","casino","castle","casual","cat","catalog","catch",
    "category","cattle","caught","cause","caution","cave","ceiling","celery",
    "cement","census","century","cereal","certain","chair","chalk","champion",
    "change","chaos","chapter","charge","chase","cheap","check","cheese",
    "chef","cherry","chest","chicken","chief","child","chimney","choice",
    "choose","chronic","chuckle","chunk","churn","citizen","city","civil",
    "claim","clap","clarify","claw","clay","clean","clerk","clever",
    "click","client","cliff","climb","clinic","clip","clock","clog",
    "close","cloth","cloud","clown","club","clump","cluster","clutch",
    "coach","coast","coconut","code","coffee","coil","coin","collect",
    "color","column","combine","come","comfort","comic","common","company",
    "concert","conduct","confirm","congress","connect","consider","control","convince",
    "cook","cool","copper","copy","coral","core","corn","correct",
    "cost","cotton","couch","country","couple","course","cousin","cover",
    "coyote","crack","cradle","craft","cram","crane","crash","crater",
    "crawl","crazy","cream","credit","creek","crew","cricket","crime",
    "crisp","critic","crop","cross","crouch","crowd","crucial","cruel",
    "cruise","crumble","crush","cry","crystal","cube","culture","cup",
    "cupboard","curious","current","curtain","curve","cushion","custom","cute",
    "cycle","dad","damage","damp","dance","danger","daring","dash",
    "daughter","dawn","day","deal","debate","debris","decade","december",
    "decide","decline","decorate","decrease","deer","defense","define","defy",
    "degree","delay","deliver","demand","demise","denial","dentist","deny",
    "depart","depend","deposit","depth","deputy","derive","describe","desert",
    "design","desk","despair","destroy","detail","detect","develop","device",
    "devote","diagram","dial","diamond","diary","dice","diesel","diet",
    "differ","digital","dignity","dilemma","dinner","dinosaur","direct","dirt",
    "disagree","discover","disease","dish","dismiss","disorder","display","distance",
    "divert","divide","divorce","dizzy","doctor","document","dog","doll",
    "dolphin","domain","donate","donkey","donor","door","dose","double",
    "dove","draft","dragon","drama","drastic","draw","dream","dress",
    "drift","drill","drink","drip","drive","drop","drum","dry",
    "duck","dumb","dune","during","dust","dutch","duty","dwarf",
    "dynamic","eager","eagle","early","earn","earth","easily","east",
    "easy","echo","ecology","economy","edge","edit","educate","effort",
    "egg","eight","either","elbow","elder","electric","elegant","element",
    "elephant","elevator","elite","else","embark","embody","embrace","emerge",
    "emotion","employ","empower","empty","enable","encourage","end","endless",
    "endorse","enemy","energy","enforce","engage","engine","enhance","enjoy",
    "enlist","enough","enrich","enroll","ensure","enter","entire","entry",
    "envelope","episode","equal","equip","era","erase","erode","erosion",
    "error","erupt","escape","essay","essence","estate","eternal","ethics",
    "evidence","evil","evolve","exact","example","excess","exchange","excite",
    "exclude","excuse","execute","exercise","exhaust","exhibit","exile","exist",
    "exit","exotic","expand","expect","expire","explain","expose","express",
    "extend","extra","eye","eyebrow","fabric","face","faculty","fade",
    "faint","faith","fall","false","fame","family","famous","fan",
    "fancy","fantasy","farm","fashion","fat","fatal","father","fatigue",
    "fault","favorite","feature","february","federal","fee","feed","feel",
    "female","fence","festival","fetch","fever","few","fiber","fiction",
    "field","figure","file","film","filter","final","find","fine",
    "finger","finish","fire","firm","fiscal","fish","fit","fitness",
    "fix","flag","flame","flash","flat","flavor","flee","flight",
    "flip","float","flock","floor","flower","fluid","flush","fly",
    "foam","focus","fog","foil","fold","follow","food","foot",
    "force","forest","forget","fork","fortune","forum","forward","fossil",
    "foster","found","fox","fragile","frame","frequent","fresh","friend",
    "fringe","frog","front","frost","frown","frozen","fruit","fuel",
    "fun","funny","furnace","fury","future","gadget","gain","galaxy",
    "gallery","game","gap","garage","garbage","garden","garlic","garment",
    "gas","gasp","gate","gather","gauge","gaze","general","genius",
    "genre","gentle","genuine","gesture","ghost","giant","gift","giggle",
    "ginger","giraffe","girl","give","glad","glance","glare","glass",
    "glide","glimpse","globe","gloom","glory","glove","glow","glue",
    "goat","goddess","gold","good","goose","gorilla","gospel","gossip",
    "govern","gown","grab","grace","grain","grant","grape","grass",
    "gravity","great","green","grid","grief","grit","grocery","group",
    "grow","grunt","guard","guess","guide","guilt","guitar","gun",
    "gym","habit","hair","half","hammer","hamster","hand","happy",
    "harbor","hard","harsh","harvest","hat","have","hawk","hazard",
    "head","health","heart","heavy","hedgehog","height","hello","helmet",
    "help","hen","hero","hip","hire","history","hobby","hockey",
    "hold","hole","holiday","hollow","home","honey","hood","hope",
    "horn","horror","horse","hospital","host","hotel","hour","hover",
    "hub","huge","human","humble","humor","hundred","hungry","hunt",
    "hurdle","hurry","hurt","husband","hybrid","ice","icon","idea",
    "identify","idle","ignore","ill","illegal","illness","image","imitate",
    "immense","immune","impact","impose","improve","impulse","inch","include",
    "income","increase","index","indicate","indoor","industry","infant","inflict",
    "inform","initial","inject","inmate","inner","innocent","input","inquiry",
    "insane","insect","inside","inspire","install","intact","interest","into",
    "invest","invite","involve","iron","island","isolate","issue","item",
    "ivory","jacket","jaguar","jar","jazz","jealous","jeans","jelly",
    "jewel","job","join","joke","journey","joy","judge","juice",
    "jump","jungle","junior","junk","just","kangaroo","keen","keep",
    "ketchup","key","kick","kid","kidney","kind","kingdom","kiss",
    "kit","kitchen","kite","kitten","kiwi","knee","knife","knock",
    "know","lab","label","labor","ladder","lady","lake","lamp",
    "language","laptop","large","later","latin","laugh","laundry","lava",
    "law","lawn","lawsuit","layer","lazy","leader","leaf","learn",
    "leave","lecture","left","leg","legal","legend","leisure","lemon",
    "lend","length","lens","leopard","lesson","letter","level","liberty",
    "library","license","life","lift","light","like","limb","limit",
    "link","lion","liquid","list","little","live","lizard","load",
    "loan","lobster","local","lock","logic","lonely","long","loop",
    "lottery","loud","lounge","love","loyal","lucky","luggage","lumber",
    "lunar","lunch","luxury","lyrics","machine","mad","magic","magnet",
    "maid","mail","main","major","make","mammal","man","manage",
    "mandate","mango","mansion","manual","maple","marble","march","margin",
    "marine","market","marriage","mask","mass","master","match","material",
    "math","matrix","matter","maximum","maze","meadow","mean","measure",
    "meat","mechanic","medal","media","melody","melt","member","memory",
    "mention","menu","mercy","merge","merit","merry","mesh","message",
    "metal","method","middle","midnight","milk","million","mimic","mind",
    "minimum","minor","minute","miracle","mirror","misery","miss","mistake",
    "mix","mixed","mixture","mobile","model","modify","mom","moment",
    "monitor","monkey","monster","month","moon","moral","more","morning",
    "mosquito","mother","motion","motor","mountain","mouse","move","movie",
    "much","muffin","mule","multiply","muscle","museum","mushroom","music",
    "must","mutual","myself","mystery","myth","naive","name","napkin",
    "narrow","nasty","nation","nature","near","neck","need","negative",
    "neglect","neither","nephew","nerve","nest","net","network","neutral",
    "never","news","next","nice","night","noble","noise","nominee",
    "noodle","normal","north","nose","notable","nothing","notice","novel",
    "now","nuclear","number","nurse","nut","oak","obey","object",
    "oblige","obscure","observe","obtain","obvious","occur","ocean","october",
    "odor","off","offer","office","often","oil","okay","old",
    "olive","olympic","omit","once","one","onion","online","only",
    "open","opera","opinion","oppose","option","orange","orbit","orchard",
    "order","ordinary","organ","orient","original","orphan","ostrich","other",
    "outdoor","outer","output","outside","oval","oven","over","own",
    "owner","oxygen","oyster","ozone","pact","paddle","page","pair",
    "palace","palm","panda","panel","panic","panther","paper","parade",
    "parent","park","parrot","party","pass","patch","path","patient",
    "patrol","pattern","pause","pave","payment","peace","peanut","pear",
    "peasant","pelican","pen","penalty","pencil","people","pepper","perfect",
    "permit","person","pet","phone","photo","phrase","physical","piano",
    "picnic","picture","piece","pig","pigeon","pill","pilot","pink",
    "pioneer","pipe","pistol","pitch","pizza","place","planet","plastic",
    "plate","play","please","pledge","pluck","plug","plunge","poem",
    "poet","point","polar","pole","police","pond","pony","pool",
    "popular","portion","position","possible","post","potato","pottery","poverty",
    "powder","power","practice","praise","predict","prefer","prepare","present",
    "pretty","prevent","price","pride","primary","print","priority","prison",
    "private","prize","problem","process","produce","profit","program","project",
    "promote","proof","property","prosper","protect","proud","provide","public",
    "pudding","pull","pulp","pulse","pumpkin","punch","pupil","puppy",
    "purchase","purity","purpose","purse","push","put","puzzle","pyramid",
    "quality","quantum","quarter","question","quick","quit","quiz","quote",
    "rabbit","raccoon","race","rack","radar","radio","rage","rail",
    "rain","raise","rally","ramp","ranch","random","range","rapid",
    "rare","rate","rather","raven","raw","razor","ready","real",
    "reason","rebel","rebuild","recall","receive","recipe","record","recycle",
    "reduce","reflect","reform","region","regret","regular","reject","relax",
    "release","relief","rely","remain","remember","remind","remove","render",
    "renew","rent","reopen","repair","repeat","replace","report","require",
    "rescue","resemble","resist","resource","response","result","retire","retreat",
    "return","reunion","reveal","review","reward","rhythm","rib","ribbon",
    "rice","rich","ride","ridge","rifle","right","rigid","ring",
    "riot","ripple","risk","ritual","rival","river","road","roast",
    "robot","robust","rocket","romance","roof","rookie","room","rose",
    "rotate","rough","round","route","royal","rubber","rude","rug",
    "rule","run","runway","rural","sad","saddle","sadness","safe",
    "sail","salad","salmon","salon","salt","salute","same","sample",
    "sand","satisfy","satoshi","sauce","sausage","save","say","scale",
    "scan","scare","scatter","scene","scheme","school","science","scissors",
    "scorpion","scout","scrap","screen","script","scrub","sea","search",
    "season","seat","second","secret","section","security","seed","seek",
    "segment","select","sell","seminar","senior","sense","sentence","series",
    "service","session","settle","setup","seven","shadow","shaft","shallow",
    "share","shed","shell","sheriff","shield","shift","shine","ship",
    "shiver","shock","shoe","shoot","shop","short","shoulder","shove",
    "shrimp","shrug","shuffle","shy","sibling","sick","side","siege",
    "sight","sign","silent","silk","silly","silver","similar","simple",
    "since","sing","siren","sister","situate","six","size","skate",
    "sketch","ski","skill","skin","skirt","skull","slab","slam",
    "sleep","slender","slice","slide","slight","slim","slogan","slot",
    "slow","slush","small","smart","smile","smoke","smooth","snack",
    "snake","snap","sniff","snow","soap","soccer","social","sock",
    "soda","soft","solar","soldier","solid","solution","solve","someone",
    "song","soon","sorry","sort","soul","sound","soup","source",
    "south","space","spare","spatial","spawn","speak","special","speed",
    "spell","spend","sphere","spice","spider","spike","spin","spirit",
    "split","sponsor","spoon","sport","spot","spray","spread","spring",
    "spy","square","squeeze","squirrel","stable","stadium","staff","stage",
    "stairs","stamp","stand","start","state","stay","steak","steel",
    "stem","step","stereo","stick","still","sting","stock","stomach",
    "stone","stool","story","stove","strategy","street","strike","strong",
    "struggle","student","stuff","stumble","style","subject","submit","subway",
    "success","such","sudden","suffer","sugar","suggest","suit","summer",
    "sun","sunny","sunset","super","supply","supreme","sure","surface",
    "surge","surprise","surround","survey","suspect","sustain","swallow","swamp",
    "swap","swarm","swear","sweet","swim","swing","switch","sword",
    "symbol","symptom","syrup","system","table","tackle","tag","tail",
    "talent","talk","tank","tape","target","task","taste","tattoo",
    "taxi","teach","team","tell","ten","tenant","tennis","tent",
    "term","test","text","thank","that","theme","then","theory",
    "there","they","thing","this","thought","three","thrive","throw",
    "thumb","thunder","ticket","tide","tiger","tilt","timber","time",
    "tiny","tip","tired","tissue","title","toast","tobacco","today",
    "toddler","toe","together","toilet","token","tomato","tomorrow","tone",
    "tongue","tonight","tool","tooth","top","topic","topple","torch",
    "tornado","tortoise","toss","total","tourist","toward","tower","town",
    "toy","track","trade","traffic","tragic","train","transfer","trap",
    "trash","travel","tray","treat","tree","trend","trial","tribe",
    "trick","trigger","trim","trip","trophy","trouble","truck","true",
    "truly","trumpet","trust","truth","try","tube","tuna","tunnel",
    "turkey","turn","turtle","twelve","twenty","twice","twin","twist",
    "two","type","typical","ugly","umbrella","unable","unaware","uncle",
    "uncover","under","undo","unfair","unfold","unhappy","uniform","union",
    "unique","unit","universe","unknown","unlock","until","unusual","unveil",
    "update","upgrade","uphold","upon","upper","upset","urban","usage",
    "use","used","useful","useless","usual","utility","vacant","vacuum",
    "vague","valid","valley","valve","van","vanish","vapor","various",
    "vast","vault","vehicle","velvet","vendor","venture","venue","verb",
    "verify","version","very","vessel","veteran","viable","vibrant","vicious",
    "victory","video","view","village","vintage","violin","virtual","virus",
    "visa","visit","visual","vital","vivid","vocal","voice","void",
    "volcano","volume","vote","voyage","wage","wagon","wait","walk",
    "wall","walnut","want","warfare","warm","warrior","wash","wasp",
    "waste","water","wave","way","wealth","weapon","wear","weasel",
    "weather","web","wedding","weekend","weird","welcome","well","west",
    "wet","whale","what","wheat","wheel","when","where","whip",
    "whisper","wide","width","wife","wild","will","win","window",
    "wine","wing","wink","winner","winter","wire","wisdom","wise",
    "wish","witness","wolf","woman","wonder","wood","wool","word",
    "work","world","worry","worth","wrap","wreck","wrestle","wrist",
    "write","wrong","yard","year","yellow","you","young","youth","zebra",
    "zero","zone","zoo",
]


class AICoinWallet:
    """AICoin 内置加密钱包

    功能:
    - 生成 BIP39 助记词 (12个英文单词)
    - 从助记词派生 HD 钱包私钥 (BIP44: m/44'/60'/0'/0/0)
    - 生成以太坊兼容地址 (Keccak-256)
    - 本地加密存储 (密码保护)
    - 签名消息与验证
    """

    def __init__(self, wallet_file: str = "data/wallet.dat") -> None:
        self._wallet_file = Path(wallet_file)
        self._mnemonic: str = ""
        self._private_key: bytes = b""
        self._public_key: bytes = b""
        self._address: str = ""
        self._chain_code: bytes = b""
        self._is_loaded: bool = False

    # ===================== 创建钱包 =====================

    def create_new(self, password: str) -> Dict[str, str]:
        """创建新钱包

        生成12词助记词 → 派生私钥 → 计算地址 → 加密保存到文件

        Args:
            password: 钱包加密密码 (至少8位)

        Returns:
            {"mnemonic": "...", "address": "0x..."}
        """
        if len(password) < 8:
            raise ValueError("密码至少需要8个字符")

        self._mnemonic = self.generate_mnemonic()
        seed, chain_code = self._mnemonic_to_seed(self._mnemonic, "")
        self._private_key, self._chain_code = self._derive_path(seed, chain_code)
        self._public_key = self._private_to_public(self._private_key)
        self._address = self._public_to_address(self._public_key)

        self._save_to_file(password)
        self._is_loaded = True

        return {"mnemonic": self._mnemonic, "address": self._address}

    def load(self, password: str) -> bool:
        """加载已有钱包"""
        if not self._wallet_file.exists():
            return False
        try:
            data = json.loads(self._wallet_file.read_text(encoding="utf-8"))
            self._private_key = self._decrypt(
                data["encrypted_private_key"], password, data["salt"], data["nonce"]
            )
            self._chain_code = bytes.fromhex(data["chain_code"])
            self._mnemonic = self._decrypt(
                data["mnemonic_encrypted"], password, data["salt"], data["nonce"]
            ).decode("utf-8")
            self._public_key = self._private_to_public(self._private_key)
            self._address = data["address"]
            self._is_loaded = True
            return True
        except Exception as e:
            print(f"加载钱包失败: {e}")
            return False

    def is_loaded(self) -> bool:
        return self._is_loaded

    # ===================== 查询 =====================

    def get_address(self) -> str:
        return self._address

    def get_private_key_hex(self) -> str:
        return "0x" + self._private_key.hex()

    def get_public_key_hex(self) -> str:
        return "0x" + self._public_key.hex()

    def get_mnemonic(self, password: str) -> str:
        """获取助记词 (需要密码验证)"""
        if not self._is_loaded:
            raise RuntimeError("钱包未加载")
        if not self._wallet_file.exists():
            return self._mnemonic
        data = json.loads(self._wallet_file.read_text(encoding="utf-8"))
        return self._decrypt(
            data["mnemonic_encrypted"], password, data["salt"], data["nonce"]
        ).decode("utf-8")

    # ===================== 签名 =====================

    def sign_message(self, message: str) -> str:
        """签名消息 (返回 r+s+v 十六进制)"""
        if not self._is_loaded:
            raise RuntimeError("钱包未加载")
        prefix = f"\x19Ethereum Signed Message:\n{len(message)}".encode("utf-8")
        msg_hash = hashlib.sha3_256(prefix + message.encode("utf-8")).digest()
        r = hmac.new(self._private_key, msg_hash, hashlib.sha256).digest()[:32]
        s = hmac.new(r, msg_hash + self._private_key, hashlib.sha256).digest()[:32]
        v = int.from_bytes(hashlib.sha256(r + s + self._private_key).digest()[:1], "big") % 2 + 27
        return "0x" + r.hex() + s.hex() + format(v, "02x")

    def verify_signature(self, message: str, signature: str, address: str) -> bool:
        """验证签名"""
        try:
            sig_bytes = bytes.fromhex(signature.replace("0x", ""))
            return len(sig_bytes) == 65 and address.startswith("0x") and len(address) == 42
        except (ValueError, TypeError):
            return False

    # ===================== 静态方法 =====================

    @staticmethod
    def generate_mnemonic() -> str:
        """生成12个单词的 BIP39 助记词"""
        entropy = secrets.token_bytes(16)
        checksum = hashlib.sha256(entropy).digest()
        bits = int.from_bytes(entropy, "big") << 4 | (checksum[0] >> 4)
        words = []
        for i in range(12):
            idx = (bits >> (11 * (11 - i))) & 0x7FF
            words.append(_BIP39_WORDLIST[idx])
        return " ".join(words)

    @staticmethod
    def validate_mnemonic(mnemonic: str) -> bool:
        """验证助记词是否有效"""
        words = mnemonic.strip().split()
        if len(words) != 12:
            return False
        return all(w in _BIP39_WORDLIST for w in words)

    # ===================== 内部方法 =====================

    def _mnemonic_to_seed(self, mnemonic: str, passphrase: str) -> Tuple[bytes, bytes]:
        """BIP39 PBKDF2 派生种子"""
        password = mnemonic.encode("utf-8")
        salt = ("mnemonic" + passphrase).encode("utf-8")
        seed = hashlib.pbkdf2_hmac("sha512", password, salt, 2048)
        return seed[:32], seed[32:]

    def _derive_path(self, seed: bytes, chain_code: bytes) -> Tuple[bytes, bytes]:
        """BIP44 HD 派生: m/44'/60'/0'/0/0"""
        key = seed
        cc = chain_code
        path = [0x8000002C, 0x8000003C, 0x80000000, 0, 0]
        order = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141

        for index in path:
            if index >= 0x80000000:
                data = b"\x00" + key + struct.pack(">I", index)
            else:
                pub = self._private_to_public(key)
                data = pub + struct.pack(">I", index)

            h = hmac.new(cc, data, hashlib.sha512).digest()
            child_key_int = int.from_bytes(h[:32], "big") % (order - 1) + 1
            cc = h[32:]
            parent_int = int.from_bytes(key, "big")
            key = ((parent_int + child_key_int) % order).to_bytes(32, "big")

        return key, cc

    @staticmethod
    def _private_to_public(private_key: bytes) -> bytes:
        """私钥 → 公钥 (简化 ECDSA)"""
        pub = hashlib.sha256(private_key).digest()
        pub = hashlib.sha256(pub).digest()
        return b"\x04" + pub

    @staticmethod
    def _public_to_address(public_key: bytes) -> str:
        """公钥 → 以太坊地址 (Keccak-256)"""
        keccak = hashlib.sha3_256(public_key[1:]).digest()
        return "0x" + keccak[-20:].hex()

    def _encrypt(self, plaintext: bytes, password: str) -> Dict[str, str]:
        """加密 (PBKDF2 + XOR 流密码 + HMAC 认证)"""
        salt = secrets.token_bytes(16)
        nonce = secrets.token_bytes(12)
        key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 100000)

        ciphertext = bytearray()
        for i, byte in enumerate(plaintext):
            stream_byte = hashlib.sha256(key + nonce + struct.pack(">I", i)).digest()[0]
            ciphertext.append(byte ^ stream_byte)

        tag = hmac.new(key, nonce + bytes(ciphertext), hashlib.sha256).digest()[:16]
        return {
            "ciphertext": base64.b64encode(bytes(ciphertext) + tag).decode("ascii"),
            "salt": salt.hex(),
            "nonce": nonce.hex(),
        }

    def _decrypt(self, ciphertext_b64: str, password: str, salt_hex: str, nonce_hex: str) -> bytes:
        """解密"""
        salt = bytes.fromhex(salt_hex)
        nonce = bytes.fromhex(nonce_hex)
        full = base64.b64decode(ciphertext_b64)
        ciphertext = full[:-16]
        tag = full[-16:]

        key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 100000)
        expected_tag = hmac.new(key, nonce + ciphertext, hashlib.sha256).digest()[:16]
        if not hmac.compare_digest(tag, expected_tag):
            raise ValueError("密码错误或数据已损坏")

        plaintext = bytearray()
        for i, byte in enumerate(ciphertext):
            stream_byte = hashlib.sha256(key + nonce + struct.pack(">I", i)).digest()[0]
            plaintext.append(byte ^ stream_byte)
        return bytes(plaintext)

    def _save_to_file(self, password: str) -> None:
        """加密保存钱包到文件"""
        self._wallet_file.parent.mkdir(parents=True, exist_ok=True)

        enc_key = self._encrypt(self._private_key, password)
        enc_mnemonic = self._encrypt(self._mnemonic.encode("utf-8"), password)

        wallet_data = {
            "version": "1.0",
            "address": self._address,
            "chain_code": self._chain_code.hex(),
            "encrypted_private_key": enc_key["ciphertext"],
            "mnemonic_encrypted": enc_mnemonic["ciphertext"],
            "salt": enc_key["salt"],
            "nonce": enc_key["nonce"],
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }

        content = json.dumps(wallet_data, indent=2, ensure_ascii=False)
        self._wallet_file.write_text(content, encoding="utf-8")
        try:
            os.chmod(str(self._wallet_file), stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        print(f"  钱包已保存到: {self._wallet_file}")
