DETAIL_COUNT=0
TIMELINE_COUNT=0
for f in ./match_data/details/*/*/*/*
do
    DETAIL_COUNT=$((DETAIL_COUNT+1))
done
for f in ./match_data/timeline/*/*/*
do
    TIMELINE_COUNT=$((TIMELINE_COUNT+1))
done
echo 'Details  : '$DETAIL_COUNT '('$(du -hs ./match_data/details | cut -f1)')'
echo 'Timeline : '$TIMELINE_COUNT '('$(du -hs ./match_data/timeline | cut -f1)')'
echo 'Postgres : '$(docker container exec lightshield-postgres-1 du -hs /var/lib/postgresql/data | cut -f1)