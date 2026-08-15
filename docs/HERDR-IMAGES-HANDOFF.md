# Estado de imágenes inline en jcode y Herdr

Fecha: 2026-08-15
Repositorio: `/Users/ferrer/ai/HUB/jcode`

## Resumen

El problema no estaba en la generación Kitty APC de jcode. Cuando jcode corre dentro de repo-term, Herdr recibe y consume las secuencias APC antes de que lleguen al renderizador. La solución implementada es detectar el entorno Herdr y enviar la imagen por el socket Unix de Herdr usando `pane.graphics.set`.

## Cambios realizados

- Archivo principal: `crates/jcode-terminal-image/src/display.rs`.
- Nuevo protocolo `ImageProtocol::Herdr`.
- Activación cuando están presentes:
  - `HERDR_ENV=1` (también acepta valores booleanos equivalentes).
  - `HERDR_SOCKET_PATH`.
  - `HERDR_PANE_ID`.
- La petición envía `format=png`, `data_base64`, ancho y alto.
- No se fuerza una posición explícita. Herdr usa la posición actual del cursor.
- Los IDs de petición son únicos por proceso para evitar colisiones.
- Si Herdr no está disponible, se conserva el comportamiento de error/fallback existente.
- Documentación añadida en `docs/HERDR.md`.

## Commits

- `460feea9c feat: render images through Herdr pane API`
- `527895b57 fix: finalize Herdr image routing`

## Verificación realizada

- `cargo test -p jcode-terminal-image`: 12 tests, todos pasan.
- `cargo build --profile selfdev`: pasa.
- `scripts/install_release.sh --fast`: pasa y termina con código 0.
- El probe real contra el socket Herdr devolvió:

```text
protocol=Herdr
display=Ok(true)
```

- La API real `pane.graphics.set` aceptó la petición en la sesión Herdr de repo-term.
- El binario instalado contiene la integración `pane.graphics.set`.

## Instalación actual

`~/.local/bin/jcode` apunta a:

```text
/Users/ferrer/.jcode/builds/current/jcode
```

Versión instalada:

```text
v0.76.19-dev (527895b57, dirty)
build_time: 2026-08-15 18:15:30 +0000
```

`dirty` es esperado porque el árbol de trabajo ya tenía cambios ajenos antes de esta tarea.

## Hallazgo importante pendiente

La instalación nueva está correcta, pero los procesos jcode ya abiertos siguen usando el binario anterior:

```text
/Users/ferrer/.jcode/builds/versions/231159361-dirty-6906f47f5f41/jcode
```

El daemon compartido y las sesiones TUI activas no deben darse por actualizados solo porque el symlink cambió. Para probar las imágenes en el flujo real hay que:

1. Guardar o cerrar las sesiones jcode abiertas.
2. Cerrar/reiniciar el cliente jcode.
3. Confirmar que el nuevo proceso usa `527895b57`.
4. Generar una imagen dentro de repo-term y comprobar que aparece inline.

No se cerraron automáticamente las sesiones activas para evitar perder trabajo del usuario.

## Comandos útiles para continuar

```bash
cd /Users/ferrer/ai/HUB/jcode
~/.local/bin/jcode version --json
scripts/install_release.sh --fast
```

Para comprobar el binario de un proceso activo en macOS:

```bash
lsof -p <PID> | grep '/jcode$'
```

La prueba de aceptación final debe hacerse con un **proceso jcode recién lanzado**, no con una sesión que ya estaba abierta antes de la instalación.
