# config/settings.py
from __future__ import annotations

from pathlib import Path
from urllib.parse import quote_plus

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


class Settings(BaseSettings):
    """Configuracion del servicio 3 (partes-persister).

    Persiste el parte extraido por sv2 y lo casa contra Sigrid:
      - trabajador  -> emp  (por DNI / codigo / nombre)
      - obra        -> obr  (por codigo / nombre)
      - codigo hora -> auxhor (por tipo_hora normal/extra y codigo leido)

    Sigrid se cablea solo si estan las 3 credenciales SIGRID_API_*. Si
    faltan, el persist sigue funcionando y el casado se omite (los
    campos *_ide quedan a NULL y el front permite resolverlos a mano).
    """

    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ------------------------------------------------------------ #
    # BBDD (PostgreSQL). DB propia 'partes' (compartida con sv4).
    # ------------------------------------------------------------ #
    # --- Azure Storage (colas/blobs del pipeline). En Azure las inyecta la
    # Container App; en local se leen del .env como el resto de config. --- #
    # Connection strings para LOCAL (Azurite o cuenta con clave). Si estan,
    # MANDAN sobre las account_url + identidad. BLOBS se deriva de COLAS si
    # falta (Azurite: puerto 10001 -> 10000).
    colas_connection_string: str | None = Field(
        None, alias="COLAS_CONNECTION_STRING"
    )
    blobs_connection_string: str | None = Field(
        None, alias="BLOBS_CONNECTION_STRING"
    )

    colas_account_url: str | None = Field(None, alias="COLAS_ACCOUNT_URL")
    blobs_account_url: str | None = Field(None, alias="BLOBS_ACCOUNT_URL")

    pg_host: str = Field("localhost", alias="PG_HOST")
    pg_port: int = Field(5432, alias="PG_PORT")
    pg_db: str = Field("partes", alias="PG_DB")
    pg_user: str = Field("postgres", alias="PG_USER")
    pg_password: str = Field(..., alias="PG_PASSWORD")

    pg_admin_db: str = Field("postgres", alias="PG_ADMIN_DB")
    pg_admin_user: str = Field("postgres", alias="PG_ADMIN_USER")
    pg_admin_password: str = Field(..., alias="PG_ADMIN_PASSWORD")

    # ------------------------------------------------------------ #
    # Sigrid (sigrid-api). Casado trabajador / obra / codigo de hora.
    # Best-effort: si faltan, el casado se omite.
    # ------------------------------------------------------------ #
    sigrid_api_base_url: str | None = Field(None, alias="SIGRID_API_BASE_URL")
    sigrid_api_function_key: str | None = Field(
        None, alias="SIGRID_API_FUNCTION_KEY"
    )
    sigrid_api_database: str | None = Field(None, alias="SIGRID_API_DATABASE")
    sigrid_api_timeout_s: float = Field(30.0, alias="SIGRID_API_TIMEOUT_S")
    sigrid_api_max_rows: int = Field(10000, alias="SIGRID_API_MAX_ROWS")

    # Empresa de Construcciones Ruesma en Sigrid (con.emp). Filtra
    # empleados / tipos de hora de la empresa correcta. 1 por defecto.
    sigrid_empresa: int = Field(1, alias="SIGRID_EMPRESA")

    # ------------------------------------------------------------ #
    # Resolucion del codigo de hora (auxhor).
    #
    # Cuando el parte NO trae un codigo de hora explicito legible, el
    # resolver mapea ``tipo_hora`` (normal/extra) al flag auxhor.ext
    # (0/1) y elige el codigo configurado aqui. Si se dejan vacios,
    # elige el PRIMER auxhor activo con ese flag ``ext``.
    # ------------------------------------------------------------ #
    default_hora_normal_cod: str | None = Field(
        None, alias="DEFAULT_HORA_NORMAL_COD"
    )
    default_hora_extra_cod: str | None = Field(
        None, alias="DEFAULT_HORA_EXTRA_COD"
    )

    # ------------------------------------------------------------ #
    # Umbrales de casado (0..1). Por debajo del minimo, el match se
    # descarta (queda sin casar y el front lo resuelve a mano).
    # ------------------------------------------------------------ #
    empleado_min_score: float = Field(0.60, alias="EMPLEADO_MIN_SCORE")
    obra_min_score: float = Field(0.55, alias="OBRA_MIN_SCORE")

    # Jornada ordinaria estandar (horas). Lo que exceda en la columna Ord.
    # se reparte a extras de forma determinista. Preparado para jornada de
    # verano (luego se podra hacer dependiente de la fecha).
    jornada_ordinaria_horas: float = Field(8.0, alias="JORNADA_ORDINARIA_HORAS")
    # CanDefecto (horas) <= a este umbral se considera NO informado en
    # Sigrid: el reparto usa la jornada por defecto en su lugar.
    candef_minimo_valido: float = Field(2.0, alias="CANDEF_MINIMO_VALIDO")

    # Calendario laboral (fin de semana + festivos). En fin de semana o
    # festivo no hay horas ordinarias: el reparto las manda todas a extra.
    # Hoy lo alimenta este JSON local; el dia de manana, Sesame.
    calendario_laboral_path: str = Field(
        "config/calendario_laboral.json", alias="CALENDARIO_LABORAL_PATH"
    )

    # ------------------------------------------------------------ #
    # SharePoint (Microsoft Graph). Archiva el PDF del parte. Best-effort:
    # si falla la subida, el parte se guarda igual en PostgreSQL (sin URL).
    # Se activa solo si hay GRAPH_KEY y la config minima del modo elegido.
    # ------------------------------------------------------------ #
    graph_key: str | None = Field(None, alias="GRAPH_KEY")
    graph_timeout_s: int = Field(60, alias="GRAPH_TIMEOUT_S")
    sharepoint_mode: str = Field("drive_id", alias="SHAREPOINT_MODE")
    sharepoint_drive_id: str | None = Field(None, alias="SHAREPOINT_DRIVE_ID")
    sharepoint_folder_url: str | None = Field(
        None, alias="SHAREPOINT_FOLDER_URL"
    )
    sharepoint_hostname: str | None = Field(None, alias="SHAREPOINT_HOSTNAME")
    sharepoint_site_path: str | None = Field(None, alias="SHAREPOINT_SITE_PATH")
    sharepoint_drive_name: str = Field(
        "Documentos", alias="SHAREPOINT_DRIVE_NAME"
    )
    # Carpeta raiz dentro del drive donde se archivan los partes. Debajo se
    # crea <root>/<YYYY>/<MM>/.
    sharepoint_folder_root: str = Field("partes", alias="SHAREPOINT_FOLDER_ROOT")
    sharepoint_create_link: bool = Field(
        True, alias="SHAREPOINT_CREATE_LINK"
    )
    sharepoint_link_type: str = Field("view", alias="SHAREPOINT_LINK_TYPE")
    sharepoint_link_scope: str = Field(
        "organization", alias="SHAREPOINT_LINK_SCOPE"
    )

    # ------------------------------------------------------------ #
    # API.
    # ------------------------------------------------------------ #
    api_host: str = Field("127.0.0.1", alias="API_HOST")
    api_port: int = Field(8011, alias="API_PORT")
    log_level: str = Field("INFO", alias="LOG_LEVEL")
    log_dir: str = Field("logs", alias="LOG_DIR")
    service_version: str = Field("1.0.0", alias="SERVICE_VERSION")

    # ------------------------------------------------------------ #
    # Derivados.
    # ------------------------------------------------------------ #
    @property
    def database_url(self) -> str:
        return (
            f"postgresql+psycopg://{self.pg_user}:{quote_plus(self.pg_password)}"
            f"@{self.pg_host}:{self.pg_port}/{self.pg_db}"
        )

    @property
    def admin_database_url(self) -> str:
        return (
            f"postgresql+psycopg://{self.pg_admin_user}:"
            f"{quote_plus(self.pg_admin_password)}"
            f"@{self.pg_host}:{self.pg_port}/{self.pg_admin_db}"
        )

    @property
    def sigrid_credentials_present(self) -> bool:
        return bool(
            (self.sigrid_api_base_url or "").strip()
            and (self.sigrid_api_function_key or "").strip()
            and (self.sigrid_api_database or "").strip()
        )

    @property
    def sigrid_catalog_enabled(self) -> bool:
        return self.sigrid_credentials_present

    @property
    def sharepoint_enabled(self) -> bool:
        if not (self.graph_key or "").strip():
            return False
        mode = (self.sharepoint_mode or "").strip().lower()
        if mode == "drive_id":
            return bool((self.sharepoint_drive_id or "").strip())
        if mode == "folder_url":
            return bool((self.sharepoint_folder_url or "").strip())
        if mode == "site_path":
            return bool(
                (self.sharepoint_hostname or "").strip()
                and (self.sharepoint_site_path or "").strip()
            )
        return False
