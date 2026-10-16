"""El tunel de la PC puente: solo se abre si el .env lo pide, apunta al
puerto de la base publicado en el loopback del VPS, y si ssh no conecta la
tarea falla con el motivo en vez de escribir en otra base."""
import pytest

from tools import tunel_puente


def test_sin_configuracion_no_abre_nada(monkeypatch):
    monkeypatch.delenv("PUENTE_TUNEL_SSH", raising=False)
    llamado = []
    monkeypatch.setattr(tunel_puente.subprocess, "Popen", lambda *a, **k: llamado.append(a))
    with tunel_puente.tunel():
        pass
    assert llamado == []


def test_el_comando_reenvia_solo_el_puerto_de_la_base(monkeypatch, tmp_path):
    monkeypatch.setenv("PUENTE_TUNEL_LLAVE", str(tmp_path / "llave"))
    cmd = tunel_puente.comando("puente@2.24.115.75")
    assert cmd[:2] == ["ssh", "-N"]
    assert "127.0.0.1:15432:127.0.0.1:5442" in cmd
    assert str(tmp_path / "llave") in cmd
    assert "BatchMode=yes" in cmd and "ExitOnForwardFailure=yes" in cmd
    assert cmd[-1] == "puente@2.24.115.75"


def test_si_ssh_no_conecta_falla_con_el_motivo(monkeypatch):
    class SshCaido:
        stderr = type("E", (), {"read": staticmethod(lambda: b"Permission denied (publickey).")})()
        def poll(self): return 255
        def terminate(self): pass
        def wait(self, timeout=None): return 255

    monkeypatch.setenv("PUENTE_TUNEL_SSH", "puente@2.24.115.75")
    monkeypatch.setattr(tunel_puente.subprocess, "Popen", lambda *a, **k: SshCaido())
    monkeypatch.setattr(tunel_puente, "_acepta", lambda puerto: False)
    with pytest.raises(RuntimeError, match="Permission denied"), tunel_puente.tunel():
        pass


def test_la_ruta_de_huellas_va_sin_comillas_si_no_hace_falta():
    """El ssh de Windows Server de la PC puente rechaza las comillas."""
    from pathlib import PureWindowsPath
    assert tunel_puente._ruta_ssh(PureWindowsPath("E:/licitapro-peru-main/data/kh")) == \
        "E:/licitapro-peru-main/data/kh"
    assert tunel_puente._ruta_ssh(PureWindowsPath("C:/Users/KEVIN GARCIA/kh")) == \
        '"C:/Users/KEVIN GARCIA/kh"'
