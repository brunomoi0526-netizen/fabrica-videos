#!/usr/bin/env bash
# Crea un video por cada guion que todavia no tenga su "Release" en GitHub.
set -uo pipefail
shopt -s nullglob
errores=0
if [ -n "${REHACER:-}" ]; then
  gh release delete "$REHACER" --yes --cleanup-tag 2>/dev/null || true
fi
for f in guiones/*.json; do
  id=$(basename "$f" .json)
  if gh release view "$id" > /dev/null 2>&1; then
    echo "Ya listo: $id"
    continue
  fi
  echo "::group::Creando $id"
  if python fabrica.py --solo "$id"; then
    dir="LISTOS PARA PUBLICAR/$id"
    titulo=$(python -c "import json,sys;print(json.load(open(sys.argv[1],encoding='utf-8'))['titulo'][:120])" "$dir/datos.json")
    gh release create "$id" "$dir"/*.mp4 "$dir/miniatura.jpg" "$dir/datos.json" \
      --title "$titulo" --notes-file "$dir/datos.json" --latest=false \
      || { echo "No se pudo publicar el release de $id"; errores=$((errores+1)); }
    rm -rf "$dir"
  else
    echo "::error::Fallo al crear $id"
    errores=$((errores+1))
  fi
  echo "::endgroup::"
done
exit $errores
