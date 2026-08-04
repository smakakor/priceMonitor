FROM apify/actor-python-playwright:3.13-1.61.0

USER myuser

COPY --chown=myuser:myuser requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=myuser:myuser . ./

ENTRYPOINT ["python3", "-m", "src"]
