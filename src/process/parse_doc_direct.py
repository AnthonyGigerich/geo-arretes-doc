"""Analyse un arrêté et en extrait les données.

"""

import argparse
from datetime import datetime
import logging
from pathlib import Path
from typing import Dict, List

import pandas as pd

from src.domain_knowledge.actes import P_ACCUSE
from src.domain_knowledge.adresse import (
    create_adresse_normalisee,
)
from src.domain_knowledge.cadastre import generate_refcadastrale_norm, get_parcelle
from src.domain_knowledge.codes_geo import get_codeinsee, get_codepostal
from src.domain_knowledge.logement import get_adr_doc, get_gest, get_proprio, get_syndic
from src.domain_knowledge.typologie_securite import (
    get_classe,
    get_demo,
    get_equ_com,
    get_int_hab,
    get_urgence,
)
from src.preprocess.data_sources import (
    EXCLUDE_FILES,
    EXCLUDE_FIXME_FILES,
    EXCLUDE_HORS_AMP,
)
from src.process.export_data import (
    DTYPE_ADRESSE,
    DTYPE_ARRETE,
    DTYPE_NOTIFIE,
    DTYPE_PARCELLE,
)
from src.process.extract_data import determine_commune
from src.process.parse_doc import parse_arrete_pages
from src.utils.str_date import process_date_brute
from src.utils.text_utils import normalize_string
from src.utils.txt_format import load_pages_text


# URL stable pour les PDF: "yyyy" sera remplacé par l'année de l'arrêté, "pdf" par le nom du fichier
FS_URL = "https://sig.ampmetropole.fr/geodata/geo_arretes/peril/{yyyy}/{pdf}"


def extract_adresses_commune(
    fn_pdf: str, pg_txt_body: str, commune_maire: str
) -> List[Dict]:
    """Extraire les adresses visées par l'arrêté, et la commune.

    Parameters
    ----------
    fn_pdf: string
        Nom du fichier PDF de l'arrêté (pour les messages de logs: warnings et erreurs)
    pg_txt_body: string
        Corps de texte de la page
    commune_maire: string
        Mention de la commune extraite de l'autorité prenant l'arrêté,
        ou des

    Returns
    -------
    adresses: list(dict)
        Adresses visées par l'arrêté
    """
    adresses_brutes = get_adr_doc(pg_txt_body)
    if not adresses_brutes:
        return []
    # prendre la 1re zone d'adresses reconnue dans le texte (heuristique)
    # TODO en repérer d'autres? incertain
    adr0 = adresses_brutes[0]
    adresse_brute = adr0["adresse_brute"]
    # TODO améliorer les résultats par une collecte plus exhaustive (qui nécessiterait le dédoublonnage) ou une meilleure heuristique ?
    # extraire la ou les adresses de cette zone
    # (on supprime au passage les préfixes "adr_" des noms des champs, archaïsme à corriger plus tard éventuellement)
    adresses = [
        ({"ad_brute": adresse_brute} | {k.replace("adr_", ""): v for k, v in x.items()})
        for x in adr0["adresses"]
    ]
    # end WIP
    if not adresses:
        logging.error(
            f"{fn_pdf}: aucune adresse extraite de la zone d'adresse(s): {adresse_brute}"
        )

    # si besoin d'une alternative: déterminer commune, code INSEE et code postal pour adresses[0] et propager les valeurs aux autres adresses
    for adresse in adresses:
        # - déterminer la commune de l'adresse visée par l'arrêté en reconciliant la commune mentionnée
        # dans cette adresse avec celle extraite des mentions de l'autorité ou du template
        adresse["ville"] = determine_commune(adresse["ville"], commune_maire)
        if not adresse["ville"]:
            logging.warning(f"{fn_pdf}: impossible de déterminer la commune")
        # - déterminer le code INSEE de la commune
        # FIXME communes hors Métropole: le filtrage sera-t-il fait en amont, lors de l'extraction depuis actes? sinon AssertionError ici
        try:
            codeinsee = get_codeinsee(adresse["ville"], adresse["cpostal"])
        except AssertionError:
            print(
                f"{fn_pdf}: get_codeinsee(): adr_ville={adresse['ville']}, adr_cpostal={adresse['cpostal']}"
            )
            print(f"{adresse}")
            raise
        if not codeinsee:
            logging.warning(f"{fn_pdf}: impossible de déterminer le code INSEE")
        # - si l'adresse ne contenait pas de code postal, essayer de déterminer le code postal
        # à partir du code INSEE de la commune (ne fonctionne pas pour Aix-en-Provence)
        if not adresse["cpostal"]:
            adresse["cpostal"] = get_codepostal(adresse["ville"], codeinsee)
            if not adresse["cpostal"]:
                logging.warning(
                    f"{fn_pdf}: Pas de code postal: adr_brute={adresse['ad_brute']}, commune={adresse['ville']}, code_insee={codeinsee}, get_codepostal={adresse['cpostal']}"
                )
        # - créer une adresse normalisée ; la cohérence des champs est vérifiée
        adresse["adresse"] = create_adresse_normalisee(
            adresse["num"],
            adresse["ind"],
            adresse["voie"],
            adresse["compl"],
            adresse["cpostal"],
            adresse["ville"],
        )
        adresse["codeinsee"] = codeinsee

    return adresses


def parse_arrete(fp_pdf_in: Path, fp_txt_in: Path) -> dict:
    """Analyse un arrêté et extrait les données qu'il contient.

    L'arrêté est découpé en paragraphes puis les données sont
    extraites.

    Parameters
    ----------
    fp_pdf_in: Path
        Fichier PDF source (temporairement?)
    fp_txt_in: Path
        Fichier texte à analyser.

    Returns
    -------
    doc_data: dict
        Données extraites du document.
    """
    fn_pdf = fp_pdf_in.name

    pages = load_pages_text(fp_txt_in)
    if not any(pages):
        logging.warning(f"{fp_txt_in}: aucune page de texte")
        return {
            "adresses": [],
            "arretes": [
                {
                    "pdf": fn_pdf,
                    "url": fp_pdf_in,  # FS_URL.format(yyyy="unk", fn_pdf)  # TODO arretes["date"].dt.year ?
                }
            ],
            "notifies": [],
            "parcelles": [],
        }

    # filtrer les pages qui sont à sortir du traitement:
    # - la ou les éventuelles pages d'accusé de réception d'actes
    pages_ar = [i for i, x in enumerate(pages, start=1) if P_ACCUSE.match(x)]
    if pages_ar:
        logging.warning(
            f"{fp_txt_in}: {len(pages_ar)} page(s) d'accusé de réception actes: {pages_ar} (sur {len(pages)})"
        )
    # - la ou les éventuelles pages d'annexes ? (TODO)
    skip_pages = pages_ar
    # remplacer les pages filtrées par une chaîne vide
    filt_pages = [
        (x if i not in skip_pages else "") for i, x in enumerate(pages, start=1)
    ]

    # analyser la structure des pages
    doc_content = parse_arrete_pages(fn_pdf, filt_pages)

    # extraire les données
    adresses = []
    arretes = {}  # un seul
    notifies = {
        "proprios": set(),  # propriétaires
        "syndics": set(),  # syndic (normalement unique)
        "gests": set(),  # gestionnaire (normalement unique)
    }
    parcelles = set()  # références de parcelles cadastrales

    # - au préalable, rassembler toutes les données en ajoutant le numéro de page (FIXME)
    pages_body = [pg_cont["body"] for pg_cont in doc_content]
    # pages_cont = [pg_cont["content"] for pg_cont in doc_content]  # future
    pages_cont = []
    for pg_num, pg_cont in enumerate(doc_content, start=1):
        # pg_template = page_cont["template"]
        # pg_content = page_cont["content"]  # future
        # FIXME ajouter "page_num" en amont, dans parse_arrete_pages()
        pages_cont.extend([({"page_num": pg_num} | x) for x in pg_cont["content"]])

    # extraire les champs un par un:
    # - arrêté
    arr_dates = [
        process_date_brute(x["span_txt"])
        for x in pages_cont
        if x["span_typ"] == "arr_date"
    ]
    if arr_dates:
        arretes["date"] = normalize_string(arr_dates[0])
    arr_nums = [x["span_txt"] for x in pages_cont if x["span_typ"] == "num_arr"]
    if arr_nums:
        arretes["num_arr"] = normalize_string(arr_nums[0])
    arr_noms = [x["span_txt"] for x in pages_cont if x["span_typ"] == "nom_arr"]
    if arr_noms:
        arretes["nom_arr"] = normalize_string(arr_noms[0])

    # - commune extraite des mentions de l'autorité prenant l'arrêté, ou du template du document
    adrs_commune_maire = [x for x in pages_cont if x["span_typ"] == "adr_ville"]
    # - prendre arbitrairement la 1re mention et la nettoyer a minima
    # TODO regarder les erreurs et vérifier si un autre choix donnerait de meilleurs résultats
    if not adrs_commune_maire:
        adr_commune_maire = None
    else:
        adr_commune_maire = normalize_string(adrs_commune_maire[0]["span_txt"])
    # print(f"commune: {adr_commune_maire}")  # DEBUG
    #
    # parcelles
    codeinsee = None  # valeur par défaut
    cpostal = None  # valeur par défaut
    for pg_txt_body in pages_body:
        if pg_txt_body:
            # extraire la ou les adresse(s) visée(s) par l'arrêté détectées sur cette page
            if not adresses:
                # pour le moment, on se contente de la première page contenant au moins une zone d'adresse,
                # et sur cette page, de la première zone d'adresse trouvée ;
                # une zone peut contenir une ou plusieurs adresses obtenues par "dépliage" (ex: 12 - 14 rue X)
                # TODO examiner les erreurs et déterminer si une autre stratégie donnerait de meilleurs résultats
                pg_adresses = extract_adresses_commune(
                    fn_pdf, pg_txt_body, adr_commune_maire
                )
                if pg_adresses:
                    adresses.extend(pg_adresses)
                    # WIP on prend le code INSEE et code postal de la 1re adresse
                    # print(adrs_doc)
                    codeinsee = adresses[0]["codeinsee"]
                    cpostal = adresses[0]["cpostal"]

            # extraire les informations sur l'arrêté
            if "classe" not in arretes:
                arretes["classe"] = get_classe(pg_txt_body)
            if "urgence" not in arretes:
                arretes["urgence"] = get_urgence(pg_txt_body)
            if "demo" not in arretes:
                arretes["demo"] = get_demo(pg_txt_body)
            if "int_hab" not in arretes:
                arretes["int_hab"] = get_int_hab(pg_txt_body)
            if "equ_com" not in arretes:
                arretes["equ_com"] = get_equ_com(pg_txt_body)
            if "pdf" not in arretes:
                arretes["pdf"] = fn_pdf
            if "url" not in arretes:
                arretes[
                    "url"
                ] = fp_pdf_in  # FS_URL.format(yyyy="unk", fn_pdf)  # TODO arretes["date"].dt.year ?
            if "codeinsee" not in arretes:
                arretes["codeinsee"] = codeinsee

            # extraire les notifiés
            proprios = get_proprio(pg_txt_body)
            if proprios is not None:
                notifies["proprios"].add(
                    normalize_string(proprios)
                )  # WIP: proprios = [] + extend()
            syndics = get_syndic(pg_txt_body)
            if syndics is not None:
                notifies["syndics"].add(
                    normalize_string(syndics)
                )  # WIP: syndics = [] + extend ?
            gests = get_gest(pg_txt_body)
            if gests is not None:
                notifies["gests"].add(
                    normalize_string(gests)
                )  # WIP: gests = [] + extend ?

            # extraire la ou les parcelles visées par l'arrêté
            pg_parcelles_str = get_parcelle(pg_txt_body)
            if pg_parcelles_str:
                refcads_norm = [
                    generate_refcadastrale_norm(
                        codeinsee, pg_parcelles_str, fn_pdf, cpostal
                    )
                ]
                parcelles = parcelles | set(refcads_norm)  # FIXME get_parcelle:list()
    if False:
        # WIP hypothèses sur les notifiés
        try:
            assert len(notifies["proprios"]) <= 1
            assert len(notifies["syndics"]) <= 1
            assert len(notifies["gests"]) <= 1
        except AssertionError:
            print(f"{notifies}")
            raise

    # si parse_content() a renvoyé [], arretes vaut toujours {} mais on veut pdf et url
    # TODO corriger, c'est moche (et un peu fragile)
    if not arretes:
        arretes = {
            "pdf": fn_pdf,
            "url": fp_pdf_in,  # FS_URL.format(yyyy="unk", fn_pdf)  # TODO arretes["date"].dt.year ?
        }

    doc_data = {
        "adresses": adresses,
        "arretes": [arretes],  # a priori un seul par fichier
        "notifies": [
            {
                "id_proprio": list(notifies["proprios"])[0]
                if notifies["proprios"]
                else None,
                "proprio": "TODO_proprio",
                "id_syndic": list(notifies["syndics"])[0]
                if notifies["syndics"]
                else None,
                "syndic": "TODO_syndic",
                "id_gest": list(notifies["gests"])[0] if notifies["gests"] else None,
                "gest": "TODO_gest",
                "codeinsee": codeinsee,
            }
        ],  # a priori un seul par fichier (pour le moment)
        "parcelles": [{"ref_cad": x, "codeinsee": codeinsee} for x in parcelles],
    }
    return doc_data


def process_files(
    in_dir_pdf: Path, in_dir_ntxt: Path, in_dir_otxt: Path, out_dir: Path
):
    """Analyse le texte des fichiers PDF extrait dans des fichiers TXT.

    Parameters
    ----------
    in_dir_pdf: Path
        Dossier contenant les fichiers PDF
    in_dir_ntxt: Path
        Dossier contenant les fichiers TXT natif
    in_dir_otxt: Path
        Dossier contenant les fichiers TXT extrait par OCR
    out_dir: Path
        Dossier destination des fichiers CSV contenant les données extraites
    """
    # date de mise à jour
    datemaj = datetime.now().date().strftime("%d/%m/%Y")

    # filtrage en deux temps, car glob() est case-sensitive (sur linux en tout cas)
    # et l'extension de certains fichiers est ".PDF" plutôt que ".pdf"
    fps_pdf = sorted(
        x
        for x in in_dir_pdf.glob("*")
        if (
            (x.suffix.lower() == ".pdf")
            and (x.name not in set(EXCLUDE_FILES + EXCLUDE_FIXME_FILES))
        )
    )

    # 4 tables de sortie
    rows_adresse = []
    rows_arrete = []
    rows_notifie = []
    rows_parcelle = []
    # itérer sur les fichiers PDF et TXT
    for i, fp_pdf in enumerate(fps_pdf):
        idu = f"id_{i:04}"  # FIXME identifiant unique
        # fichier txt
        fp_otxt = in_dir_otxt / f"{fp_pdf.stem}.txt"  # ocr
        fp_ntxt = in_dir_ntxt / f"{fp_pdf.stem}.txt"  # natif
        # if fp_otxt.is_file():
        #     # texte ocr
        #     fp_txt = fp_otxt
        # elif fp_ntxt.is_file():
        if fp_ntxt.is_file():
            # sinon texte natif
            fp_txt = fp_ntxt
        else:
            # sinon anomalie
            fp_txt = None
            raise ValueError(f"Aucun fichier txt trouvé pour {fp_pdf}")
        # print(f"---------\n{fp_pdf}")  # DEBUG
        doc_data = parse_arrete(fp_pdf, fp_txt)

        # ajout des entrées dans les 4 tables
        rows_adresse.extend(
            ({"idu": idu} | x | {"datemaj": datemaj}) for x in doc_data["adresses"]
        )
        rows_arrete.extend(
            ({"idu": idu} | x | {"datemaj": datemaj}) for x in doc_data["arretes"]
        )
        rows_notifie.extend(
            ({"idu": idu} | x | {"datemaj": datemaj}) for x in doc_data["notifies"]
        )
        rows_parcelle.extend(
            ({"idu": idu} | x | {"datemaj": datemaj}) for x in doc_data["parcelles"]
        )
    # créer les 4 DataFrames
    for key, rows, dtype in [
        ("adresse", rows_adresse, DTYPE_ADRESSE),
        ("arrete", rows_arrete, DTYPE_ARRETE),
        ("notifie", rows_notifie, DTYPE_NOTIFIE),
        ("parcelle", rows_parcelle, DTYPE_PARCELLE),
    ]:
        out_file = out_files[key]
        df = pd.DataFrame.from_records(rows).astype(dtype=dtype)
        df.to_csv(out_file, index=False)
        # TODO copier le fichier CSV en ajoutant au nom de fichier la date de traitement


if __name__ == "__main__":
    # log
    dir_log = Path(__file__).resolve().parents[2] / "logs"
    logging.basicConfig(
        filename=f"{dir_log}/test_parse_doc_direct_{datetime.now().isoformat()}.log",
        encoding="utf-8",
        level=logging.DEBUG,
    )

    # arguments de la commande exécutable
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "in_dir_pdf",
        help="Chemin vers le dossier contenant les fichiers PDF",
    )
    parser.add_argument(
        "in_dir_ntxt",
        help="Chemin vers le dossier contenant les fichiers TXT de texte natif",
    )
    parser.add_argument(
        "in_dir_otxt",
        help="Chemin vers le dossier contenant les fichiers TXT de texte extrait par OCR",
    )
    parser.add_argument(
        "out_dir",
        help="Chemin vers le dossier pour les 4 fichiers CSV en sortie contenant les données extraites des documents",
    )
    # par défaut, le fichier out_file ne doit pas exister, sinon option:
    # "redo" (écrase le fichier existant)
    parser.add_argument(
        "--redo",
        action="store_true",
        help="Ré-exécuter le traitement d'un lot, et écraser le fichier de sortie",
    )
    args = parser.parse_args()

    # entrée: fichiers PDF et TXT
    in_dir_pdf = Path(args.in_dir_pdf).resolve()
    if not in_dir_pdf.is_dir():
        raise ValueError(f"Impossible de trouver le dossier {in_dir_pdf}")
    in_dir_ntxt = Path(args.in_dir_ntxt).resolve()
    if not in_dir_ntxt.is_dir():
        raise ValueError(f"Impossible de trouver le dossier {in_dir_ntxt}")
    in_dir_otxt = Path(args.in_dir_otxt).resolve()
    if not in_dir_otxt.is_dir():
        raise ValueError(f"Impossible de trouver le dossier {in_dir_otxt}")

    # sortie: CSV de documents
    # on crée le dossier parent (récursivement) si besoin
    out_dir = Path(args.out_dir).resolve()
    out_files = {
        x: out_dir / f"paquet_{x}.csv"
        for x in ["arrete", "adresse", "parcelle", "notifie"]
    }
    if out_dir.is_dir():
        for out_file in out_files.values():
            if out_file.is_file():
                if not args.redo:
                    # erreur si le fichier CSV existe déjà mais ni redo, ni append
                    raise ValueError(
                        f"Le fichier de sortie {out_file} existe déjà. Pour l'écraser, ajoutez --redo ; pour l'augmenter, ajoutez --append."
                    )
    else:
        # créer le dossier de sortie si besoin
        logging.info(
            f"Dossier de sortie: {out_dir} {'existe déjà' if out_dir.is_dir() else 'doit être créé'}."
        )
        out_dir.mkdir(exist_ok=True)

    process_files(in_dir_pdf, in_dir_ntxt, in_dir_otxt, out_dir)