#!/usr/bin/env python3
"""
Extrai os dados do dump do QSA da Receita Federal

O arquivo de origem usado tem o nome "F.K032001K.D81106A.zip", mas pode ser
especificado qualquer arquivo de origem que siga o mesmo padrão - esse arquivo
não está disponível no site da Receita Federal (obtive pela Lei de Acesso à
Informação).

Dentro do arquivo zip existe apenas um arquivo do tipo fixed-width file,
contendo registros de diversas tabelas diferentes. O script descompacta sob
demanda o zip e, conforme vai lendo os registros contidos, cria os arquivos de
saída, em formato CSV. Você deve especificar o arquivo de entrada e o diretório
onde ficarão os CSVs de saída (que por padrão ficam compactados também, em
gzip, para diminuir o tempo de escrita e economizar espaço em disco).

Se você quer apenas acesso aos dados convertidos, você não precisa baixar
o arquivo de entrada e rodar o script (que pode levar horas) - procure por esse
dataset em https://brasil.io/ e baixe os dados:
    https://drive.google.com/open?id=1tOGB1mJZcF5V1SUS-YlPJF0-zdhfN1yd
"""

import argparse
import io
from zipfile import ZipFile
from pathlib import Path

import rows
from rows.fields import slug
from rows.plugins.utils import ipartition
from rows.utils import CsvLazyDictWriter, open_compressed
from tqdm import tqdm


# Fields to delete/clean in some cases so we don't expose personal information
# TODO: add option to not delete/clear these fields
fields_to_delete = ("codigo_pais", "correio_eletronico", "filler", "nome_pais")
fields_to_clear_if_mei = (
    "complemento",
    "ddd_fax",
    "ddd_telefone_1",
    "ddd_telefone_2",
    "descricao_tipo_logradouro",
    "logradouro",
    "numero",
)


class ParsingError(ValueError):
    def __init__(self, line, error):
        super().__init__()
        self.line = line
        self.error = error


def clear_company_name(name):
    """
    >>> clear_company_name('FALANO DE TAL 12345678901')
    'FALANO DE TAL'
    >>> clear_company_name('FALANO DE TAL CPF 12345678901')
    'FALANO DE TAL'
    >>> clear_company_name('FALANO DE TAL - CPF 12345678901')
    'FALANO DE TAL'
    >>> clear_company_name('123456')
    '123456'
    """
    if name.isdigit():  # Weird name, but not an "eupresa"
        return name

    words = name.split()
    if words[-1].isdigit() and len(words[-1]) == 11:  # Remove CPF (numbers)
        words.pop()
    if words[-1].upper() == "CPF":  # Remove CPF (word)
        words.pop()
    if words[-1] == "-":
        words.pop()
    return " ".join(words).strip()


def clear_email(email):
    """
    >>> clear_email('-') is None
    True
    >>> clear_email('.') is None
    True
    >>> clear_email('0') is None
    True
    >>> clear_email('0000000000000000000000000000000000000000') is None
    True
    >>> clear_email('N/TEM') is None
    True
    >>> clear_email('NAO POSSUI') is None
    True
    >>> clear_email('NAO TEM') is None
    True
    >>> clear_email('NT') is None
    True
    >>> clear_email('S/N') is None
    True
    >>> clear_email('XXXXXXXX') is None
    True
    >>> clear_email('________________________________________') is None
    True
    >>> clear_email('n/t') is None
    True
    >>> clear_email('nao tem') is None
    True
    """

    clean = email.lower().replace("/", "").replace("_", "")
    if len(set(clean)) < 3 or clean in ("nao tem", "n tem", "ntem", "nao possui", "nt"):
        return None
    return email


def read_header(filename):
    """Read a CSV file which describes a fixed-width file

    The CSV must have the following columns:

    - name (final field name)
    - size (fixed size of the field, in bytes)
    - start_column (column in the fwf file where the fields starts)
    - type ("A" for text, "N" for int)
    """

    table = rows.import_from_csv(filename)
    table.order_by("start_column")
    header = []
    for row in table:
        row = dict(row._asdict())
        row["field_name"] = slug(row["name"])
        row["start_index"] = row["start_column"] - 1
        row["end_index"] = row["start_index"] + row["size"]
        header.append(row)
    return header


def transform_empresa(row):
    """Transform row of type company"""

    row["correio_eletronico"] = clear_email(row["correio_eletronico"])

    if row["opcao_pelo_simples"] in ("", "0", "6", "8"):
        row["opcao_pelo_simples"] = "0"
    elif row["opcao_pelo_simples"] in ("5", "7"):
        row["opcao_pelo_simples"] = "1"
    else:
        raise ValueError(
            f"Opção pelo Simples inválida: {row['opcao_pelo_simples']} (CNPJ: {row['cnpj']})"
        )

    if set(row["nome_fantasia"]) == set(["0"]):
        row["nome_fantasia"] = ""

    if row["opcao_pelo_mei"] in ("N", ""):
        row["opcao_pelo_mei"] = "0"
    elif row["opcao_pelo_mei"] == "S":
        row["opcao_pelo_mei"] = "1"

        # Clear CPF from razao_social/nome_fantasia
        if row["razao_social"].split()[-1].isdigit():
            row["razao_social"] = clear_company_name(row["razao_social"])
        if row["nome_fantasia"] and row["nome_fantasia"].split()[-1].isdigit():
            row["nome_fantasia"] = clear_company_name(row["nome_fantasia"])

        # Clear all fields which can expose personal info
        for field_name in fields_to_clear_if_mei:
            row[field_name] = ""

    else:
        raise ValueError(
            f"Opção pelo MEI inválida: {row['opcao_pelo_mei']} (CNPJ: {row['cnpj']})"
        )

    # Clear all fields which can expose sensitive info
    for field_name in fields_to_delete:
        if field_name in row:
            del row[field_name]

    return [row]


def transform_socio(row):
    """Transform row of type partner"""

    assert row["campo_desconhecido"] == ""  # Always empty
    del row["campo_desconhecido"]

    if row["nome_representante_legal"] == "CPF INVALIDO":
        row["cpf_representante_legal"] = None
        row["nome_representante_legal"] = None
        row["codigo_qualificacao_representante_legal"] = None

    if row["cnpj_cpf_do_socio"] == "000***000000**":
        row["cnpj_cpf_do_socio"] = ""

    if row["identificador_de_socio"] == 2:  # Pessoa Física
        row["cnpj_cpf_do_socio"] = row["cnpj_cpf_do_socio"][-11:]

    return [row]


def transform_cnae_secundaria(row):
    """Transform row of type CNAE"""

    cnaes = [
        "".join(digits)
        for digits in ipartition(row.pop("cnae"), 7)
        if set(digits) != set(["0"])
    ]
    data = []
    for cnae in cnaes:
        new_row = row.copy()
        new_row["cnae"] = cnae
        data.append(new_row)

    return data


def parse_row(header, line):
    """Parse a fixed-width file line and returns a dict, based on metadata

    The `header` parameter is the return from `read_header`.
    Notes:
    1- There's no check whether all fields are parsed (this function trusts
       the `header` was created in the correct way).
    2- `line` is already decoded and since the input encoding is `latin1`, one
       character equals to one byte. If the input encoding does not have this
       characteristic then this function needs to be changed.
    """
    line = line.replace("\x00", " ").replace("\x02", " ")
    row = {}
    for field in header:
        field_name = field["field_name"]
        value = line[field["start_index"] : field["end_index"]].strip()

        if field_name == "filler":
            if value.strip() not in ("", "9999999999999999"):
                raise ParsingError(line=line, error="Wrong filler")
            continue  # Do not save `filler`
        elif field_name in ("tipo_de_registro", "tipo_do_registro"):
            row_type = value
            continue  # Do not save row type (will be saved in separate files)
        elif field_name in ("fim", "fim_registro", "fim_de_registro"):
            if value.strip() != "F":
                raise ParsingError(line=line, error="Wrong end")
            continue  # Do not save row end mark
        elif field_name in (
            "indicador_full_diario",
            "tipo_atualizacao",
            "tipo_de_atualizacao",
        ):
            continue  # These fields are usually useless

        if field_name.startswith("data_") and value:
            if len(str(value)) > 8:
                raise ParsingError(line=line, error="Wrong date size")
            value = f"{value[:4]}-{value[4:6]}-{value[6:8]}"
            if value == "0000-00-00":
                value = ""
        elif field["type"] == "N" and "*" not in value:
            try:
                value = int(value) if value else None
            except ValueError:
                raise ParsingError(
                    line=line, error=f"Cannot convert {repr(value)} to int"
                )

        row[field_name] = value

    return row


def extract_files(
    filename,
    header_definitions,
    transform_functions,
    output_writers,
    error_filename,
    input_encoding="latin1",
):
    """Extract files from a fixed-width file containing more than one row type

    The input filename is expected to be a zip file having only one file
    inside. The file is read and metadata inside `fobjs` is used to parse it
    and save the output files.
    """
    # TODO: use another strategy to open this file (like using rows'
    # open_compressed)
    zf = ZipFile(filename)
    filenames = zf.filelist
    assert (
        len(filenames) == 1
    ), f"Only one file inside the zip is expected (got {len(filenames)})"
    # XXX: The current approach of decoding here and then extracting
    # fixed-width-file data will work only for encodings where 1 character is
    # represented by 1 byte, such as latin1. If the encoding can represent one
    # character using more than 1 byte (like UTF-8), this approach will make
    # incorrect results.
    fobj = io.TextIOWrapper(zf.open(filenames[0]), encoding=input_encoding)

    error_fobj = open_compressed(error_filename, mode="w", encoding="latin1")
    error_writer = CsvLazyDictWriter(error_fobj)
    for line in tqdm(fobj):
        row_type = line[0]
        try:
            row = parse_row(header_definitions[row_type], line)
        except ParsingError as exception:
            error_writer.writerow({"error": exception.error, "line": exception.line})
            continue

        data = transform_functions[row_type](row)
        for row in data:
            output_writers[row_type].writerow(row)
    error_fobj.close()
    fobj.close()
    zf.close()


def main():
    base_path = Path(__file__).parent
    output_path = base_path / "data" / "output"
    error_filename = output_path / "errors.csv"

    parser = argparse.ArgumentParser()
    parser.add_argument("input_filename")
    parser.add_argument("output_path", default=str(output_path))
    args = parser.parse_args()

    input_encoding = "latin1"
    output_encoding = "utf-8"
    input_filename = args.input_filename
    output_path = Path(args.output_path)
    if not output_path.exists():
        output_path.mkdir(parents=True)
    error_filename = output_path / "error.csv.gz"

    row_types = {
        "0": {
            "header_filename": "headers/header.csv",
            "output_filename": output_path / "header.csv.gz",
            "transform_function": lambda row: [row],
        },
        "1": {
            "header_filename": "headers/empresa.csv",
            "output_filename": output_path / "empresa.csv.gz",
            "transform_function": transform_empresa,
        },
        "2": {
            "header_filename": "headers/socio.csv",
            "output_filename": output_path / "socio.csv.gz",
            "transform_function": transform_socio,
        },
        "6": {
            "header_filename": "headers/cnae-secundaria.csv",
            "output_filename": output_path / "cnae-secundaria.csv.gz",
            "transform_function": transform_cnae_secundaria,
        },
        "9": {
            "header_filename": "headers/trailler.csv",
            "output_filename": output_path / "trailler.csv.gz",
            "transform_function": lambda row: [row],
        },
    }
    header_definitions, output_writers, transform_functions = {}, {}, {}
    for row_type, data in row_types.items():
        header_definitions[row_type] = read_header(data["header_filename"])
        output_writers[row_type] = CsvLazyDictWriter(data["output_filename"])
        transform_functions[row_type] = data["transform_function"]
    extract_files(
        filename=input_filename,
        header_definitions=header_definitions,
        transform_functions=transform_functions,
        output_writers=output_writers,
        error_filename=error_filename,
        input_encoding=input_encoding,
    )


if __name__ == "__main__":
    main()
