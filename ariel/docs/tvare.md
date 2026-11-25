# Tváře: architektura a ochrana dat

Modul `faces/` v Arielu umí zapsat tvář, uložit ji, porovnat celou databázi samu se sebou, označit možné duplicity ke kontrole člověkem a mazat. Spouští se přes `tvare.bat`, poprvé `tools\tvare_install.bat`.

## Tok dat

```
kamera (XREAL Eye / webkamera / fotka v lab)
   │  snímek jen v paměti, nikdy na disk
   ▼
YuNet: najde tvář ──► kontrola kvality (velikost ≥ 64 px, jistota, ostrost)
   ▼
SFace: zarovnaný výřez 112×112 ──► embedding 128 čísel (normalizovaný)
   │  z kamery se průměruje 5 dobrých snímků do jednoho záznamu
   ▼
trezor data/faces/faces.vault  (AES-256-GCM, klíč chráněný Windows DPAPI)
   ▼
porovnání všech se všemi (kosinová podobnost, matice N×N)
   ▼
skupiny podezřelých duplicit ──► "pending" ──► rozhodne člověk (review)
                                                 ├─ stejná osoba: označit, volitelně ponechat jen 1 záznam
                                                 └─ různé osoby: pár se už nikdy znovu nehlásí
```

## Moduly

| Soubor | Co dělá |
|---|---|
| `engine.py` | detekce (YuNet) a embedding (SFace) přes OpenCV, jen CPU, modely ~37 MB, licence Apache-2.0/MIT |
| `vault.py` | šifrovaný trezor, zápis/mazání, retence, zahození výřezů |
| `keystore.py` | klíč: DPAPI (Windows), jinde heslo `ARIEL_FACES_PASSPHRASE`; skartace klíče |
| `dedupe.py` | porovnání DB samé se sebou, skupiny (union-find), rozhodnutí člověka |
| `audit.py` | log akcí `data/faces/audit.log` bez biometrických dat, jen pseudonymní ID |
| `cli.py` | příkazy `python -m faces …` (viz `tvare.bat`) |

## Režimy

| | `lab` (doma, dobrovolníci se souhlasem) | `field` (venku) |
|---|---|---|
| ukládá se | embedding, volitelně malý šifrovaný výřez (`--thumb`) pro kontrolu, poznámka | **jen embedding** |
| fotky / výřezy | jen s `--thumb`, šifrovaně v trezoru | kód je odmítne uložit (PermissionError) |
| název zdrojového souboru, poznámka | ano | ne |
| zápis z fotek | ano | ne, jen kamera |
| retence | 720 h (30 dní) | 24 h |

Přepnutí do `field` smaže všechny dříve uložené výřezy. Retence se kontroluje při každém otevření trezoru, prošlé záznamy se smažou samy.

## Prahy

SFace: kosinová podobnost ≥ 0,363 = „pravděpodobně stejná osoba“ (doporučení OpenCV), 0,30–0,363 = „možná“. Obě úrovně jdou jen ke kontrole. Mění se v `data/faces/config.json`. Před ostrým použitím je potřeba prahy ověřit na vlastních datech z XREAL Eye (jiná kamera, úhel, světlo), protože falešné shody jsou u podobných lidí a dětí častější.

## Ochrana dat (GDPR čl. 9, biometrie)

Embedding sice není fotka, ale je to biometrický údaj sloužící k jedinečné identifikaci, takže spadá pod čl. 9 GDPR stejně jako fotka.

* **Šifrování v klidu:** celý trezor je AES-256-GCM, klíč chrání DPAPI účtu Windows. Zkopírovaný soubor na jiném PC nejde přečíst.
* **Snadné mazání:** `delete ID`, automaticky retencí, nebo `wipe` = přepsání a smazání trezoru i klíče (kryptografická skartace). Při každém uložení se trezor přepíše celý, smazaný záznam v něm nezůstane.
* **Minimalizace:** venku žádné fotky, žádné názvy souborů, žádná jména; ID je náhodný pseudonym.
* **Žádné automatické rozhodnutí:** systém jen označí, rozhoduje člověk (čl. 22).
* **Audit:** kdo co zapsal, smazal, rozhodl; bez biometrie.
* **Embeddingy z jiného modelu nejsou přenositelné:** při výměně modelu se trezor musí vytvořit znovu.

## Právní upozornění k nasazení venku

Zpracování biometrie cizích lidí na ulici bez jejich výslovného souhlasu je podle GDPR v zásadě zakázané (čl. 9 odst. 1) a výjimky (čl. 9 odst. 2) se na soukromé použití brýlí téměř nevztahují. Navíc AI Act (nařízení EU 2024/1689) zakazuje vytváření databází tváří necíleným sběrem z internetu nebo kamerových záznamů (čl. 5 odst. 1 písm. e), k čemuž má plošné snímání lidí na ulici blízko, a biometrickou identifikaci na dálku řadí mezi vysoce rizikové systémy. Před použitím na lidi, kteří nedali souhlas, je potřeba: posouzení vlivu (DPIA, čl. 35), jasný právní titul (typicky výslovný souhlas), viditelné informování lidí, a ideálně konzultace s právníkem nebo ÚOOÚ. Bezpečné je zatím používat režim `lab` na lidech, kteří souhlas dali.

## Další kroky

1. Ověřit modely na Windows (`tools\tvare_install.bat`) a změřit prahy na pár dobrovolnících přes XREAL Eye.
2. Napojit na jádro Ariel: tlačítko brýlí → zápis, a decentní Glass karta „ke kontrole: N“ v HUD (bez jmen, bez fotek).
3. Volitelně silnější model (ArcFace přes OpenVINO na NPU) se stejným rozhraním `FaceEngine.faces()`.
4. Kontrola duplicit v tabletu místo příkazové řádky.
