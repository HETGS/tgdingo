from django.db import models


class Source(models.Model):
    """Read-only catalog row for an astrophysical target."""

    srcid = models.IntegerField(primary_key=True)
    nExtract = models.IntegerField(blank=True, null=True)
    source_name = models.CharField(max_length=50, db_column="object", blank=True, null=True)
    simbad_ID = models.CharField(max_length=50, blank=True, null=True)
    ned_ID = models.CharField(max_length=50, blank=True, null=True)
    ra = models.FloatField()
    decl = models.FloatField()
    pType = models.IntegerField(blank=True, null=True)
    reject = models.CharField(max_length=1, blank=True, null=True)
    cType = models.CharField(max_length=3, blank=True, null=True)
    i2mass = models.CharField(max_length=1, blank=True, null=True)
    idss1 = models.CharField(max_length=1, blank=True, null=True)
    idss2 = models.CharField(max_length=1, blank=True, null=True)

    class Meta:
        managed = False
        db_table = "source"


class Observation(models.Model):
    """Read-only observation row for a target exposure."""

    id = models.IntegerField(primary_key=True)
    qid = models.IntegerField(blank=True, null=True)
    grpid = models.IntegerField(blank=True, null=True)
    srcid = models.IntegerField(blank=True, null=True)
    obsid = models.IntegerField()
    obi = models.IntegerField(blank=True, null=True)
    target = models.CharField(max_length=50, blank=True, null=True)
    pi = models.CharField(max_length=50, blank=True, null=True)
    instrument = models.CharField(max_length=7)
    grating = models.CharField(max_length=4)
    detector_aimpoint = models.CharField(max_length=50, blank=True, null=True)
    readmode = models.CharField(max_length=20, blank=True, null=True)
    datamode = models.CharField(max_length=50, blank=True, null=True)
    x = models.FloatField(blank=True, null=True)
    y = models.FloatField(blank=True, null=True)
    ra = models.FloatField(blank=True, null=True)
    decl = models.FloatField(blank=True, null=True)
    ra_targ = models.FloatField(blank=True, null=True)
    dec_targ = models.FloatField(blank=True, null=True)
    date_obs = models.DateTimeField(blank=True, null=True)
    proc_date = models.DateTimeField(blank=True, null=True)
    exposure = models.FloatField(blank=True, null=True)
    events = models.IntegerField(blank=True, null=True)
    zo_method = models.CharField(max_length=10, blank=True, null=True)
    windowed = models.CharField(max_length=1, blank=True, null=True)
    extended = models.CharField(max_length=1, blank=True, null=True)
    multiple_sources = models.CharField(max_length=1, blank=True, null=True)
    pileup = models.IntegerField(blank=True, null=True)
    period = models.IntegerField(blank=True, null=True)
    phase = models.IntegerField(blank=True, null=True)
    dtycycle = models.IntegerField(blank=True, null=True)
    ciao_version = models.CharField(max_length=11, blank=True, null=True)
    finzero_version = models.CharField(max_length=11, blank=True, null=True)
    caldb_version = models.CharField(max_length=11, blank=True, null=True)
    tgcat_version = models.CharField(max_length=11, blank=True, null=True)
    proc_version = models.IntegerField(blank=True, null=True)
    review = models.CharField(max_length=2, blank=True, null=True)
    grpflg = models.CharField(max_length=1, blank=True, null=True)
    reject = models.CharField(max_length=1, blank=True, null=True)
    notes = models.BinaryField(blank=True, null=True)
    comment = models.BinaryField(blank=True, null=True)
    vv_sci = models.CharField(max_length=255, blank=True, null=True)
    vv_date = models.DateTimeField(blank=True, null=True)

    class Meta:
        managed = False
        db_table = "obsid"


class FileRecord(models.Model):
    """Read-only product file metadata from the TGCat files table."""

    id = models.IntegerField(primary_key=True)
    type = models.CharField(max_length=10)
    name = models.CharField(max_length=10)
    arc = models.CharField(max_length=50, blank=True, null=True)
    value = models.CharField(max_length=50, blank=True, null=True)
    size = models.IntegerField(blank=True, null=True)
    checksum = models.CharField(max_length=255, blank=True, null=True)

    class Meta:
        managed = False
        db_table = "files"
