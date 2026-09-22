# Migração final do `spulidar/measurements`

Este bundle substitui os bundles anteriores. Ele implementa:

- estado persistente por ano em `publisher/state/YYYY.json`;
- bootstrap inicial lendo **o inventário real do Cloudflare R2**;
- dashboards diários novos usando tanto imagens históricas quanto canônicas, sem expor essa distinção ao usuário;
- `sant` apresentado honestamente como `00–06 + 18–24` até existirem os dois períodos canônicos `00` e `18`;
- atualização incremental: um produto canônico já publicado continua conhecido mesmo depois que o WEBP local for apagado;
- redirects pequenos para os antigos `saam/sapm/sant_Dashboard.html` e `Gallery.html`;
- relatório seguro de objetos históricos do R2 que já podem ser removidos;
- deleção explícita do R2 somente após verificar que os objetos canônicos substitutos realmente existem no bucket;
- filtro `--publish-year YYYY` / `--publish-date YYYYMMDD` para publicar apenas o que foi reprocessado.

## Arquivos do bundle

Copie/substitua no repositório `measurements`:

```text
update-site.py
ql-measurement-calendar.html
publisher/__init__.py
publisher/catalog.py
publisher/state.py        # novo
publisher/render.py
publisher/publish.py
tests/test_catalog.py
tests/test_state.py       # novo
```

`requirements.txt` e `config.yaml` atuais já são compatíveis.

---

# Fase 0 — segurança

Comece com o repositório atualizado e uma branch de migração:

```bash
cd ~/milgrau/measurements
git pull origin main
git checkout -b publisher-state-migration
```

Descompacte o bundle por cima do checkout e confira:

```bash
git status
python -m pytest -q
```

Os testes do bundle devem passar antes do bootstrap.

---

# Fase 1 — bootstrap da memória persistente a partir do R2

**Faça isto antes de substituir os HTMLs históricos por redirects.**

```bash
python update-site.py --bootstrap-state-from-r2 --html-only --no-push
```

O comando usa `credentials.py` apenas para listar o bucket e cria:

```text
publisher/state/
├── 2012.json
├── 2013.json
├── ...
├── 2025.json
└── 2026.json
```

Ele reconhece diretamente os objetos que existem no R2:

```text
# históricos
2020/Quicklook_20200126saam_532nm_AN_15km.webp
2020/GlobalMeanRCS_20200126saam.webp

# canônicos
2020/01/20200126/rcs_20200126_spu_06_532nm_AN_15km.webp
2020/01/20200126/rcs_20200126_spu_06_mean.webp
```

Portanto você **não precisa ter os WEBPs históricos localmente**.

O bootstrap também regenera os dashboards diários e `measurements.json` usando apenas o novo estado.

Valide o resultado:

```bash
python update-site.py --validate-state
```

Depois confira o diff:

```bash
git status
git diff --stat
```

É útil abrir alguns estados:

```bash
less publisher/state/2020.json
less publisher/state/2024.json
```

---

# Fase 2 — teste local antes de tocar nos HTMLs antigos

```bash
python -m http.server 8000
```

Abra:

```text
http://localhost:8000/ql-measurement-calendar.html
```

Teste pelo menos:

1. um dia antigo com `saam/sapm/sant`;
2. um dia que só tenha `sant`;
3. `2020-01-26` ou outro dia que já possua produtos canônicos.

A interface pública não deve mostrar `legacy`, `canonical`, `saam`, `sapm` ou `sant`.

Um dia puramente histórico completo aparece como:

```text
[ 00–06 + 18–24 ] [ 06–12 ] [ 12–18 ]
```

Se `06` já tiver sido reprocessado, o botão continua visualmente `06–12`, mas a imagem usada passa a ser o `rcs_*` novo.

Quando `00` **e** `18` existirem canonicamente, o botão combinado da noite desaparece e vira:

```text
[ 00–06 ] [ ... ] [ 18–24 ]
```

---

# Fase 3 — trocar os HTMLs antigos por redirects

Somente depois de confirmar o bootstrap e os dashboards novos:

```bash
python update-site.py --write-legacy-redirects --html-only --no-push
```

Exemplo:

```text
2020/20200126saam_Dashboard.html
```

passa a ser um HTML minúsculo que redireciona para:

```text
2020/01/20200126/index.html
```

As URLs antigas continuam funcionando, mas os HTMLs antigos deixam de armazenar a interface antiga.

Teste uma URL antiga diretamente no navegador local.

---

# Fase 4 — commit inicial da migração

Quando estiver tudo certo:

```bash
git add -A
git commit -m "Add persistent R2 publication state and unified daily dashboards"
git push -u origin publisher-state-migration
```

Depois você pode revisar/mergear a branch em `main`. Se preferir trabalhar direto no `main`, faça o merge local e push normalmente.

---

# Fluxo normal de reprocessamento depois da migração

A partir daqui, `publisher/state/YYYY.json` é a memória de publicação.

Você pode apagar WEBPs canônicos locais antigos depois que já foram publicados. O publisher não esquece deles.

## Exemplo: reprocessar somente 2024

Rode o MILGRAU normalmente para gerar os novos quicklooks de 2024. Depois:

```bash
cd ~/milgrau/measurements
python update-site.py --publish-year 2024 --no-push
```

Esse comando:

1. lê o estado persistente já commitado;
2. procura os `rcs_*` locais de 2024;
3. faz upload dos períodos modificados para `YYYY/MM/YYYYMMDD/` no R2;
4. **só grava o novo estado de um período se todos os uploads daquele período tiverem sucesso**;
5. atualiza `publisher/state/2024.json`;
6. regenera apenas os dashboards cujo fingerprint mudou;
7. atualiza `measurements.json`.

Depois teste e publique:

```bash
python -m http.server 8000

git status
git diff --stat
git add -A
git commit -m "Publish reprocessed 2024 lidar quicklooks"
git push
```

## Exemplo: reprocessar só um dia

```bash
python update-site.py --publish-date 20241018 --no-push
```

Assim você não precisa tocar em outros anos que estejam presentes no seu disco.

## Se não usar filtro

```bash
python update-site.py --no-push
```

O script verifica todos os `rcs_*` canônicos locais e publica os períodos cujo fingerprint local mudou.

Na primeira execução sem filtro após o bootstrap, períodos canônicos que ainda estejam localmente podem ser reenviados uma vez. Isso é intencional e seguro: mesmo filename não é tratado como prova de mesmo conteúdo.

---

# Como a memória evita perder publicações antigas

Suponha que você publique `20241018_spu_06` hoje e depois apague os WEBPs locais de 2024.

O estado continua contendo algo equivalente a:

```json
{
  "canonical": {
    "06": {
      "channels": {
        "532nm_AN": {
          "15": "2024/10/20241018/rcs_20241018_spu_06_532nm_AN_15km.webp"
        }
      },
      "mean": "2024/10/20241018/rcs_20241018_spu_06_mean.webp"
    }
  }
}
```

Meses depois, publicar 2018 não altera essa informação. O site continua sabendo que 2024/06 já está no R2.

---

# Limpeza segura do R2

O publisher só considera um produto histórico removível quando existe substituição canônica equivalente:

```text
saam   → pode sair quando 06 existe canonicamente
sapm   → pode sair quando 12 existe canonicamente
sant   → só pode sair quando 00 E 18 existem canonicamente
```

Além disso, antes de deletar, o script consulta novamente o R2 e verifica se os objetos canônicos substitutos realmente existem no bucket.

## 1. Gerar relatório de limpeza

Depois de reprocessar 2024:

```bash
python update-site.py --cleanup-report 2024
```

O relatório fica em:

```text
logs/r2-cleanup-report-2024.json
```

`logs/` já está no `.gitignore` do repositório.

O relatório mostra:

- produtos históricos candidatos;
- quais estão realmente elegíveis;
- objetos históricos presentes;
- objetos canônicos usados como substitutos;
- qualquer substituto que esteja faltando no R2;
- quantidade aproximada de bytes removíveis.

Leia antes de apagar:

```bash
less logs/r2-cleanup-report-2024.json
```

## 2. Simular a deleção

```bash
python update-site.py --prune-r2 2024 --confirm-r2-delete --dry-run --no-push
```

Nada é deletado nesse modo.

## 3. Apagar de verdade

Quando o relatório estiver correto:

```bash
python update-site.py --prune-r2 2024 --confirm-r2-delete --no-push
```

O script:

1. lista novamente o R2;
2. verifica os substitutos canônicos;
3. apaga somente os objetos históricos elegíveis;
4. marca esses produtos como `retired` no `publisher/state/2024.json`;
5. regenera os dashboards afetados para sincronizar fingerprints;
6. nunca apaga objetos `rcs_*` canônicos.

Depois:

```bash
python update-site.py --validate-state
git status
git diff
```

E então commit/push do novo estado:

```bash
git add -A
git commit -m "Retire superseded 2024 historical R2 assets"
git push
```

---

# O que pode ser apagado e quando

Depois do bootstrap e dos redirects:

- os **conteúdos antigos** dos Dashboard/Gallery HTML já não são necessários;
- os próprios caminhos antigos são mantidos apenas como redirects por compatibilidade;
- os WEBPs históricos do R2 continuam necessários enquanto algum período ainda depender deles;
- quando o cleanup report considerar um produto elegível, ele já pode ser removido do R2 com `--prune-r2`;
- os WEBPs canônicos locais podem ser apagados depois da publicação, porque o estado persistente guarda seus object keys.

Não apague `publisher/state/`. Ele passa a ser parte importante e versionada do repositório.

---

# Recuperação / rollback

A branch criada no início deixa o estado anterior intacto em `main` até o merge.

Depois do merge, o Git também mantém todo o histórico dos dashboards antigos. Se algo inesperado ocorrer, você pode reverter o commit da migração sem depender dos arquivos locais de imagens.

Para o R2, faça sempre `--cleanup-report` + `--dry-run` antes de `--prune-r2`. A deleção do R2 é a única parte que não deve ser tratada como facilmente reversível.
