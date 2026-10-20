#!/usr/bin/env python3
"""Publish the verified iOS build without access to the private source repository."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read_manifest(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    for key, pattern in (("version", r"\d+\.\d+\.\d+"),
                         ("sourceCommit", r"[0-9a-f]{40}"),
                         ("artifactSha256", r"[0-9a-f]{64}")):
        require(isinstance(data.get(key), str) and re.fullmatch(pattern, data[key]),
                "Campo inválido no manifesto: " + key)
    require(type(data.get("artifactSize")) is int and data["artifactSize"] > 0,
            "Tamanho do artefato ausente ou inválido")
    url = data.get("artifactUrl", "")
    require(isinstance(url, str) and not any(c.isspace() for c in url), "URL inválida")
    parsed = urllib.parse.urlsplit(url)
    require(parsed.scheme == "https" and parsed.hostname is not None
            and parsed.hostname.endswith(".oaiusercontent.com")
            and parsed.username is None and parsed.password is None
            and parsed.port in (None, 443) and not parsed.fragment,
            "A origem deve ser a URL temporária do pacote exportado")
    return data


def download(data, destination):
    # This link grants access only to the build intended for public distribution.
    # Do not echo it in logs or include it in release assets/notes.
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print("::add-mask::" + data["artifactUrl"], flush=True)
    digest = hashlib.sha256()
    size = 0
    request = urllib.request.Request(data["artifactUrl"], headers={
        "User-Agent": "MundosFragmentados-iOS-publisher"})
    try:
        with urllib.request.urlopen(request, timeout=60) as source, destination.open("xb") as target:
            while chunk := source.read(1024 * 1024):
                size += len(chunk)
                require(size <= data["artifactSize"], "Download excedeu o tamanho esperado")
                digest.update(chunk)
                target.write(chunk)
    except urllib.error.HTTPError as error:
        raise ValueError(f"Download recusado (HTTP {error.code}); renove a URL temporária") from None
    except urllib.error.URLError:
        raise ValueError("Não foi possível transferir o pacote; confira a validade da URL") from None
    require(size == data["artifactSize"], "Download incompleto")
    require(digest.hexdigest() == data["artifactSha256"], "SHA-256 do artefato divergente")


def validate_archive(data, archive_path, output):
    stem = "MundosFragmentados-" + data["version"] + "-iOS-sideload"
    ipa_name, report_name = stem + ".ipa", stem + ".json"
    allowed = {ipa_name, report_name, "SOURCE-COMMIT.txt", "INSTALAR-IPHONE.txt"}
    output.mkdir()
    with zipfile.ZipFile(archive_path) as archive:
        names = archive.namelist()
        require(len(names) == len(allowed) and set(names) == allowed,
                "O artefato contém arquivos ausentes, duplicados ou inesperados")
        require(archive.getinfo(report_name).file_size < 131072, "Relatório inválido")
        require(archive.getinfo("SOURCE-COMMIT.txt").file_size < 128, "Commit inválido")
        require(0 < archive.getinfo(ipa_name).file_size < 2 * 1024 ** 3,
                "O IPA precisa ter menos de 2 GiB para ser anexado a uma release")
        report = json.loads(archive.read(report_name))
        source_commit = archive.read("SOURCE-COMMIT.txt").decode("ascii").strip()
        require(source_commit == data["sourceCommit"], "Commit do pacote divergente")
        require(report.get("version") == data["version"] and report.get("platform") == "iOS"
                and report.get("architecture") == "arm64" and report.get("signed") is False
                and report.get("sideloadRequired") is True, "Relatório não corresponde ao IPA de teste")
        for name in (ipa_name, report_name, "SOURCE-COMMIT.txt"):
            with archive.open(name) as source, (output / name).open("xb") as target:
                shutil.copyfileobj(source, target)
    ipa = output / ipa_name
    digest = hashlib.sha256()
    with ipa.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    require(ipa.stat().st_size == report.get("size") and digest.hexdigest() == report.get("sha256"),
            "Tamanho ou SHA-256 do IPA divergente")
    (output / "SHA256SUMS.txt").write_text(digest.hexdigest() + "  " + ipa_name + "\n", encoding="ascii")
    return ipa, output / report_name


def main():
    require(len(sys.argv) == 2, "Uso: publish_ios.py distribution/release.json")
    data = read_manifest(sys.argv[1])
    repository = os.environ.get("PUBLIC_REPOSITORY", "")
    public_commit = os.environ.get("PUBLIC_COMMIT", "")
    require(repository == "devgamerg-jpg/mundosfragmentadosios", "Repositório público inesperado")
    require(re.fullmatch(r"[0-9a-f]{40}", public_commit), "Commit público inválido")
    with tempfile.TemporaryDirectory(prefix="mf-ios-release-") as temporary:
        stage = Path(temporary)
        archive_path = stage / "artifact.zip"
        download(data, archive_path)
        output = stage / "release"
        ipa, report = validate_archive(data, archive_path, output)
        guide = output / "INSTALAR-IPHONE.txt"
        shutil.copyfile(Path(__file__).resolve().parents[1] / guide.name, guide)
        notes = stage / "release-notes.md"
        notes.write_text(
            "Versão de teste do Mundos Fragmentados para iPhone e iPad.\n\n"
            "Baixe o arquivo `.ipa` no Windows e instale pelo Sideloadly com o iPhone conectado por USB. "
            "As instruções estão no arquivo `INSTALAR-IPHONE.txt`.\n\n"
            "Requer iOS 15 ou posterior. O pacote é compilado para ARM64 e precisa da assinatura "
            "pessoal feita pelo Sideloadly. Com conta Apple gratuita, renove a assinatura a cada 7 dias.\n\n"
            "A compilação e a integridade do pacote foram verificadas; os testes no aparelho ainda "
            "precisam ser feitos. Esta versão não foi publicada na App Store.\n\n"
            "Código de origem do binário: `" + data["sourceCommit"] + "`.\n",
            encoding="utf-8")
        tag = "v" + data["version"] + "-ios-test"
        subprocess.run([
            "gh", "release", "create", tag, str(ipa), str(report), str(guide),
            str(output / "SOURCE-COMMIT.txt"), str(output / "SHA256SUMS.txt"),
            "--repo", repository, "--target", public_commit,
            "--title", "Mundos Fragmentados " + data["version"] + " — teste no iPhone",
            "--notes-file", str(notes), "--prerelease", "--latest=false"], check=True)
        print("Release de teste publicada: " + tag)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, zipfile.BadZipFile, OSError) as error:
        print("Falha na publicação: " + str(error), file=sys.stderr)
        sys.exit(1)
