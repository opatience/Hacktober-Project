-- Optional display names for known Purdue club accounts. Any other account gets a club automatically.
-- No posts, events, admission prices, dates or user follows are generated.
USE ROLE HACKATHON_OWNER;
MERGE INTO HACKATHON_DB.APP.CLUBS AS target
USING (
    SELECT COLUMN1 AS USERNAME, COLUMN2 AS CLUB_NAME FROM VALUES
        ('purduehackers', 'Purdue Hackers'),
        ('purdueusb', 'CS Undergraduate Student Board'),
        ('buildpurdue', 'Build Purdue'),
        ('helloworldpurdue', 'Hello World Purdue')
) AS source ON target.SLUG = source.USERNAME
WHEN MATCHED THEN UPDATE SET target.NAME = source.CLUB_NAME,
    target.UPDATED_AT = CURRENT_TIMESTAMP()
WHEN NOT MATCHED THEN INSERT (CLUB_ID, SLUG, NAME, IS_ACTIVE)
VALUES ('club-' || source.USERNAME, source.USERNAME, source.CLUB_NAME, TRUE);

MERGE INTO HACKATHON_DB.APP.INSTAGRAM_ACCOUNTS AS target
USING (
    SELECT CLUB_ID, SLUG AS USERNAME FROM HACKATHON_DB.APP.CLUBS
    WHERE SLUG IN ('purduehackers', 'purdueusb', 'buildpurdue', 'helloworldpurdue')
) AS source ON target.USERNAME = source.USERNAME
WHEN MATCHED AND target.CLUB_ID IS NULL THEN UPDATE
    SET target.CLUB_ID = source.CLUB_ID, target.UPDATED_AT = CURRENT_TIMESTAMP()
WHEN NOT MATCHED THEN INSERT (ACCOUNT_ID, USERNAME, CLUB_ID, PROFILE_URL)
VALUES ('account-' || source.USERNAME, source.USERNAME,
        source.CLUB_ID, 'https://www.instagram.com/' || source.USERNAME || '/');
