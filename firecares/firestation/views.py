import json
import ogr
import os
import osr
import pandas as pd
import shutil
import urllib
import uuid
from django.views.generic import DetailView, ListView, TemplateView, View
from django.shortcuts import get_object_or_404, redirect
from django.contrib import messages
from django.contrib.contenttypes.models import ContentType
from django.core.urlresolvers import reverse
from django.http.response import HttpResponseRedirect, HttpResponse
from django.db import connection
from django.db.models import Max, Min
from django.db.models.fields import FieldDoesNotExist
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from django.db.models import IntegerField
from django.utils.encoding import smart_str
from firecares.firecares_core.mixins import LoginRequiredMixin, CacheMixin
from firecares.firestation.managers import Ntile, Case, When
from firecares.firestation.models import FireStation, FireDepartment, Staffing
from firecares.usgs.models import (StateorTerritoryHigh, CountyorEquivalent,
    Reserve, NativeAmericanArea, IncorporatedPlace,
    UnincorporatedPlace, MinorCivilDivision)
from tempfile import mkdtemp
from firecares.tasks.cleanup import remove_file



class DISTScoreContextMixin(object):
    @staticmethod
    def add_dist_values_to_context():
        context = {}
        score_metrics = FireDepartment.objects.all().aggregate(Max('dist_model_score'), Min('dist_model_score'))
        context['dist_max'] = score_metrics['dist_model_score__max']
        context['dist_min'] = score_metrics['dist_model_score__min']

        population_metrics = FireDepartment.objects.all().aggregate(Max('population'), Min('population'))
        context['population_max'] = population_metrics['population__max'] or 0
        context['population_min'] = population_metrics['population__min'] or 0

        return context


class FeaturedDepartmentsMixin(object):
    """
    Mixin to add featured departments to a request.
    """

    @staticmethod
    def get_featured_departments():
        return FireDepartment.priority_departments.all()


class DepartmentDetailView(LoginRequiredMixin, CacheMixin, DISTScoreContextMixin, DetailView):
    model = FireDepartment
    template_name = 'firestation/department_detail.html'
    page = 1
    objects_per_page = 10
    cache_timeout = 60 * 15
    cache_permission_differentiators = ['firestation.change_firedepartment']

    def get_context_data(self, **kwargs):
        context = super(DepartmentDetailView, self).get_context_data(**kwargs)

        page = self.request.GET.get('page')

        paginator = Paginator(context['firedepartment'].firestation_set.order_by('station_number'), self.objects_per_page)

        try:
            stations = paginator.page(page)
        except PageNotAnInteger:
            # If page is not an integer, deliver first page.
            stations = paginator.page(1)
        except EmptyPage:
            # If page is out of range (e.g. 9999), deliver last page of results.
            stations = paginator.page(paginator.num_pages)

        context['firestations'] = stations

        # population stats provide summary statistics for fields within the current objects population class
        context['population_stats'] = self.object.population_class_stats
        population_quartiles = self.object.population_metrics_table

        if population_quartiles:
            # risk model fire count breaks for the bullet chart
            vals = population_quartiles.objects.get_field_stats('residential_fires_avg_3_years',
                                                                group_by='residential_fires_avg_3_years_quartile')
            context['residential_fires_avg_3_years_breaks'] = [n['max'] for n in vals]

            # size 2 or above fire breaks for the bullet chart
            vals = population_quartiles.objects.get_field_stats('risk_model_size1_percent_size2_percent_sum',
                                                                group_by='risk_model_size1_percent_size2_percent_sum_quartile')
            context['risk_model_greater_than_size_2_breaks'] = [n['max'] for n in vals]

            # deaths and injuries for the bullet chart
            vals = population_quartiles.objects.get_field_stats('risk_model_deaths_injuries_sum',
                                                                group_by='risk_model_deaths_injuries_sum_quartile')
            context['risk_model_deaths_injuries_breaks'] = [n['max'] for n in vals]

            # This should be a table with risk quartiles already identified
            report_card_peers = population_quartiles.objects.all()

            # this should be an object that has the current department quartile values
            object_values = self.object.population_metrics_row

            report_card_peers = report_card_peers.annotate(dist_model_residential_fires_quartile=Case(When(
                **{'dist_model_score__isnull': False,
                   'residential_fires_avg_3_years_quartile': object_values.residential_fires_avg_3_years_quartile,
                   'then': Ntile(4, output_field=IntegerField(),
                                 partition_by='dist_model_score is not null, residential_fires_avg_3_years_quartile',
                                 order_by='dist_model_score')}), output_field=IntegerField(), default=None))
            report_card_peers = report_card_peers.annotate(dist_model_risk_model_greater_than_size_2_quartile=Case(When(
                **{'dist_model_score__isnull': False,
                   'risk_model_size1_percent_size2_percent_sum_quartile': object_values.risk_model_size1_percent_size2_percent_sum_quartile,
                   'then': Ntile(4, output_field=IntegerField(),
                                 partition_by='dist_model_score is not null, risk_model_size1_percent_size2_percent_sum_quartile',
                                 order_by='dist_model_score')}), output_field=IntegerField(), default=None))
            report_card_peers = report_card_peers.annotate(dist_model_risk_model_deaths_injuries_quartile=Case(When(
                **{'dist_model_score__isnull': False,
                   'risk_model_deaths_injuries_sum_quartile': object_values.risk_model_deaths_injuries_sum_quartile,
                   'then': Ntile(4, output_field=IntegerField(),
                                 partition_by='dist_model_score is not null, risk_model_deaths_injuries_sum_quartile',
                                 order_by='dist_model_score')}), output_field=IntegerField(), default=None))

            df = pd.DataFrame(list(report_card_peers.values('id',
                                                            'dist_model_score',
                                                            'dist_model_residential_fires_quartile',
                                                            'dist_model_risk_model_greater_than_size_2_quartile',
                                                            'dist_model_risk_model_deaths_injuries_quartile')))

            context[
                'dist_model_risk_model_greater_than_size_2_quartile_avg'] = df.dist_model_risk_model_greater_than_size_2_quartile.mean()
            context[
                'dist_model_risk_model_deaths_injuries_quartile_avg'] = df.dist_model_risk_model_deaths_injuries_quartile.mean()
            context['dist_model_residential_fires_quartile_avg'] = df.dist_model_residential_fires_quartile.mean()

            context['dist_model_risk_model_greater_than_size_2_quartile_breaks'] = \
                df.groupby(['dist_model_risk_model_greater_than_size_2_quartile']).max()['dist_model_score'].tolist()
            context['dist_model_risk_model_deaths_injuries_quartile_breaks'] = \
                df.groupby(['dist_model_risk_model_deaths_injuries_quartile']).max()['dist_model_score'].tolist()
            context['dist_model_residential_fires_quartile_breaks'] = \
                df.groupby(['dist_model_residential_fires_quartile']).max()['dist_model_score'].tolist()

            context['dist_model_residential_fires_quartile'] = \
                df.loc[df['id'] == self.object.id].dist_model_residential_fires_quartile.values[0]
            context['dist_model_risk_model_greater_than_size_2_quartile'] = \
                df.loc[df['id'] == self.object.id].dist_model_risk_model_greater_than_size_2_quartile.values[0]
            context['dist_model_risk_model_deaths_injuries_quartile'] = \
                df.loc[df['id'] == self.object.id].dist_model_risk_model_deaths_injuries_quartile.values[0]

            # national_risk_band
            from django.db import connections
            cursor = connections['default'].cursor()
            query = FireDepartment.objects.filter(dist_model_score__isnull=False).as_quartiles().values('id',
                                                                                                        'risk_model_size1_percent_size2_percent_sum_quartile',
                                                                                                        'risk_model_deaths_injuries_sum_quartile').query.__str__()

            qu = """
            WITH results as (
            SELECT "firestation_firedepartment"."id",
             "firestation_firedepartment"."dist_model_score",
            CASE WHEN ("firestation_firedepartment"."risk_model_fires_size1_percentage" IS NOT NULL OR "firestation_firedepartment"."risk_model_fires_size2_percentage" IS NOT NULL) THEN ntile(4) over (partition by COALESCE(risk_model_fires_size1_percentage,0)+COALESCE(risk_model_fires_size2_percentage,0) != 0 order by COALESCE(risk_model_fires_size1_percentage,0)+COALESCE(risk_model_fires_size2_percentage,0)) ELSE NULL END AS "risk_model_size1_percent_size2_percent_sum_quartile", CASE WHEN ("firestation_firedepartment"."risk_model_deaths" IS NOT NULL OR "firestation_firedepartment"."risk_model_injuries" IS NOT NULL) THEN ntile(4) over (partition by COALESCE(risk_model_deaths,0)+COALESCE(risk_model_injuries,0) != 0 order by COALESCE(risk_model_deaths,0)+COALESCE(risk_model_injuries,0)) ELSE NULL END AS "risk_model_deaths_injuries_sum_quartile" FROM "firestation_firedepartment" WHERE "firestation_firedepartment"."dist_model_score" IS NOT NULL ORDER BY "firestation_firedepartment"."name" ASC
            ),
              row as (
            SELECT * from results where results.id={id}
            )

            select ntile_results.ntile
            from
            (select results.id, ntile(4) over (order by results.dist_model_score asc)
            from results
            inner join row on results.{field}=row.{field}) as ntile_results
            where ntile_results.id={id};

            """
            cursor.execute(qu.format(query=query.strip(), id=self.object.id,
                                     field='risk_model_size1_percent_size2_percent_sum_quartile'))
            try:
                context['national_risk_model_size1_percent_size2_percent_sum_quartile'] = cursor.fetchone()[0]
            except (KeyError, TypeError):
                context['national_risk_model_size1_percent_size2_percent_sum_quartile'] = None

            cursor.execute(
                qu.format(query=query.strip(), id=self.object.id, field='risk_model_deaths_injuries_sum_quartile'))

            try:
                context['national_risk_model_deaths_injuries_sum_quartile'] = cursor.fetchone()[0]
            except (KeyError, TypeError):
                context['national_risk_model_deaths_injuries_sum_quartile'] = None

        context.update(self.add_dist_values_to_context())
        return context


class DepartmentUpdateGovernmentUnits(LoginRequiredMixin, DetailView):
    """
    View to update a Department's government unit.
    """
    model = FireDepartment
    template_name = 'firestation/department_update_government_units.html'

    def _associated_government_unit_ids(self, model_type):
        return self.object.government_unit.filter(object_type=ContentType.objects.get_for_model(model_type)).values_list('object_id', flat=True)

    def get_context_data(self, **kwargs):
        context = super(DepartmentUpdateGovernmentUnits, self).get_context_data(**kwargs)

        geom = self.object.headquarters_geom.buffer(0.01)

        context['current_incorporated_places'] = self._associated_government_unit_ids(IncorporatedPlace)
        context['incorporated_places'] = IncorporatedPlace.objects.filter(geom__intersects=geom)
        context['current_minor_civil_divisions'] = self._associated_government_unit_ids(MinorCivilDivision)
        context['minor_civil_divisions'] = MinorCivilDivision.objects.filter(geom__intersects=geom)
        context['current_native_american_areas'] = self._associated_government_unit_ids(NativeAmericanArea)
        context['native_american_areas'] = NativeAmericanArea.objects.filter(geom__intersects=geom)
        context['current_reserves'] = self._associated_government_unit_ids(Reserve)
        context['reserves'] = Reserve.objects.filter(geom__intersects=geom)
        context['current_unincorporated_places'] = self._associated_government_unit_ids(UnincorporatedPlace)
        context['unincorporated_places'] = UnincorporatedPlace.objects.filter(geom__intersects=geom)
        context['current_counties'] = self._associated_government_unit_ids(CountyorEquivalent)
        context['counties'] = CountyorEquivalent.objects.filter(geom__intersects=geom)

        return context

    def post(self, request, *args, **kwargs):
        self.object = self.get_object()
        context = self.get_context_data()

        incorporated_places_selections = map(int, request.POST.getlist('incorporated_places'))
        minor_civil_divisions_selections = map(int, request.POST.getlist('minor_civil_divisions'))
        native_american_areas_selections = map(int, request.POST.getlist('native_american_areas'))
        reserves_selections = map(int, request.POST.getlist('reserves'))
        unincorporated_places_selections = map(int, request.POST.getlist('unincorporated_places'))
        counties_selections = map(int, request.POST.getlist('counties'))

        def _update_govt_units(selections, current, model_type):
            for i in set(selections) | set(current):
                if i in selections and i not in current:
                    self.object.government_unit.connect(model_type.objects.get(pk=i))
                elif i not in selections and i in current:
                    self.object.government_unit.filter(object_id=i, object_type=ContentType.objects.get_for_model(model_type)).delete()

        _update_govt_units(counties_selections, context['current_counties'], CountyorEquivalent)
        _update_govt_units(unincorporated_places_selections, context['current_unincorporated_places'], UnincorporatedPlace)
        _update_govt_units(reserves_selections, context['current_reserves'], Reserve)
        _update_govt_units(native_american_areas_selections, context['current_native_american_areas'], NativeAmericanArea)
        _update_govt_units(minor_civil_divisions_selections, context['current_minor_civil_divisions'], MinorCivilDivision)
        _update_govt_units(incorporated_places_selections, context['current_incorporated_places'], IncorporatedPlace)

        if request.POST.get('update_geom'):
            self.object.set_geometry_from_government_unit()

        messages.add_message(request, messages.SUCCESS, 'Government unit associations updated')

        return redirect(self.object)


class SafeSortMixin(object):
    """
    Allow queryset sorting on explicit fields.
    """
    # A list of tuples containing the order_by string and verbose name
    sort_by_fields = []

    def model_field_valid(self, field, choices=None):
        """
        Ensures a model field is valid.
        """
        if not field:
            return False

        if choices and field not in choices:
            return False

        if hasattr(self, 'model'):
            try:
                self.model._meta.get_field(field.replace('-', '', 1))
            except FieldDoesNotExist:
                return False

        return True

    def get_queryset(self):
        """
        Runs the sortqueryset method on the current queryset.
        """
        queryset = super(SafeSortMixin, self).get_queryset()
        return self.sort_queryset(queryset, self.request.GET.get('sortBy'))

    def sort_queryset(self, queryset, order_by):
        """
        Sorts a queryset based after ensuring the provided field is valid.
        """
        if self.model_field_valid(order_by, choices=[name for name, verbose_name in self.sort_by_fields]):
            queryset = queryset.order_by(order_by)

            if order_by == '-population':
                queryset = queryset.extra(select={'population_is_null': 'population IS NULL'}) \
                    .order_by('population_is_null', '-population')

        # default sorting to -population and put null values at the end.
        else:
            queryset = queryset.extra(select={'population_is_null': 'population IS NULL'}) \
                .order_by('population_is_null', '-population')

        return queryset

    def get_sort_context(self, context):
        """
        Adds sorting context to the context object.
        """
        context['sort_by_fields'] = []

        for field, verbose_name in self.sort_by_fields:
            get_params = self.request.GET.copy()
            get_params['sort_by'] = field
            context['sort_by_fields'].append(dict(name=verbose_name, field=field))

        context['sort_by_fields'] = json.dumps(context['sort_by_fields'])
        return context


class LimitMixin(object):
    limit_by_amounts = [15, 30, 60, 90]

    def limit_queryset(self, limit):
        """
        Limits the queryset.
        """

        try:
            limit = int(limit)

            # make sure the limit is not 0
            if limit:
                self.paginate_by = limit

        except:
            return

    def get_queryset(self):
        """
        Runs the sortqueryset method on the current queryset.
        """
        queryset = super(LimitMixin, self).get_queryset()
        return self.limit_queryset(queryset, self.request.GET.get('limit'))

    def get_limit_context(self, context):
        """
        Adds sorting context to the context object.
        """

        context['limit_by_amounts'] = []
        get_params = self.request.GET.copy()

        for limit in self.limit_by_amounts:
            get_params['limit'] = limit
            context['limit_by_amounts'].append((self.request.path + '?' + urllib.urlencode(get_params), limit))

        return context


class FireDepartmentListView(LoginRequiredMixin, ListView, SafeSortMixin, LimitMixin, DISTScoreContextMixin,
                             FeaturedDepartmentsMixin):
    model = FireDepartment
    paginate_by = 30
    queryset = FireDepartment.objects.all()
    sort_by_fields = [
        ('name', 'Name Ascending'),
        ('-name', 'Name Descending'),
        ('state', 'State Acscending'),
        ('-state', 'State Descending'),
        ('dist_model_score', 'Lowest DIST Score'),
        ('-dist_model_score', 'Highest DIST Score'),
        ('population', 'Smallest Population'),
        ('-population', 'Largest Population')
    ]

    search_fields = ['fdid', 'state', 'region', 'name']
    range_fields = ['population', 'dist_model_score']

    def handle_search(self, queryset):
        # If there is a 'q' argument, this is a full text search.
        if self.request.GET.get('q'):
            queryset = queryset.full_text_search(self.request.GET.get('q'))

        queryset = self.sort_queryset(queryset, self.request.GET.get('sortBy'))
        self.limit_queryset(self.request.GET.get('limit'))

        for field, value in self.request.GET.items():
            if value and value.lower() != 'any' and field in self.search_fields:
                if field.lower().endswith('name'):
                    field += '__icontains'
                queryset = queryset.filter(**{field: value})

            # range is passed as pair of comma delimited min and max values for example 12,36
            try:
                if field in self.range_fields and value and "," in value:
                    min, max = value.split(",")
                    Min = int(min)
                    Max = int(max)

                    if Min:
                        queryset = queryset.filter(**{field + '__gte': Min})

                    if Max:
                        from django.db.models import Q
                        queryset = queryset.filter(Q(**{field + '__lte': Max}) | Q(**{field + '__isnull': True}))

            except:
                pass
        return queryset

    def get_queryset(self):
        queryset = super(FireDepartmentListView, self).get_queryset()

        queryset = self.handle_search(queryset)

        return queryset

    def get_context_data(self, **kwargs):
        context = super(FireDepartmentListView, self).get_context_data(**kwargs)
        context = self.get_sort_context(context)
        context.update(self.add_dist_values_to_context())
        context['featured_departments'] = self.get_featured_departments().order_by('?')[:5]

        page_obj = context['page_obj']
        paginator = page_obj.paginator
        min_page = page_obj.number - 5
        min_page = max(1, min_page)
        max_page = page_obj.number + 6
        max_page = min(paginator.num_pages, max_page)
        context['windowed_range'] = range(min_page, max_page)

        context['dist_min'] = 0

        if min_page > 1:
            context['first_page'] = 1
        if max_page < paginator.num_pages:
            context['last_page'] = paginator.num_pages

        return context


class SimilarDepartmentsListView(FireDepartmentListView, CacheMixin):
    """
    Implements the Similar Department list view.
    """

    cache_timeout = 60 * 60 * 24

    def get_queryset(self):
        department = get_object_or_404(FireDepartment, pk=self.kwargs.get('pk'))
        queryset = department.similar_departments
        queryset = self.handle_search(queryset)
        return queryset


class FireStationDetailView(LoginRequiredMixin, CacheMixin, DetailView):
    model = FireStation

    cache_timeout = 60 * 60 * 24


class SpatialIntersectView(ListView):
    model = FireStation
    template_name = 'firestation/department_detail.html'
    context_object_name = 'firestations'

    def get_queryset(self):
        self.object = get_object_or_404(StateorTerritoryHigh, state_name__iexact=self.kwargs.get('state'))
        return FireStation.objects.filter(geom__intersects=self.object.geom)

    def get_context_data(self, **kwargs):
        context = super(SpatialIntersectView, self).get_context_data(**kwargs)
        context['object'] = self.object
        return context


class SetDistrictView(DetailView):
    model = CountyorEquivalent
    template_name = 'firestation/set_department.html'

    def get_context_data(self, **kwargs):
        context = super(SetDistrictView, self).get_context_data(**kwargs)
        context['stations'] = FireStation.objects.filter(geom__intersects=self.object.geom)
        context['incorporated_places'] = IncorporatedPlace.objects.filter(geom__intersects=self.object.geom)
        next_fs = FireStation.objects.filter(department__isnull=True, state='VA').order_by('?')

        if next_fs:
            context['next'] = CountyorEquivalent.objects.filter(geom__intersects=next_fs[0].geom)[0]
        return context

    def post(self, request, *args, **kwargs):
        county = self.get_object()
        try:
            fd = FireDepartment.objects.get(content_type=ContentType.objects.get_for_model(CountyorEquivalent),
                                            object_id=county.id)
        except FireDepartment.DoesNotExist:
            fd = FireDepartment.objects.create(
                name='{0} {1} Fire Department'.format(county.county_name, county.get_fcode_display()),
                content_object=county,
                geom=county.geom)
            fd.save()
            FireStation.objects.filter(geom__intersects=county.geom).update(department=fd)
            return HttpResponseRedirect(reverse('set_fire_district', args=[county.id]))


class Stats(LoginRequiredMixin, TemplateView):
    template_name = 'firestation/firestation_stats.html'

    def get_context_data(self, **kwargs):
        context = super(Stats, self).get_context_data(**kwargs)
        context['stations'] = FireStation.objects.all()
        context['departments'] = FireDepartment.objects.all()
        context['stations_with_fdid'] = FireStation.objects.filter(fdid__isnull=False)
        context['stations_with_departments'] = FireStation.objects.filter(department__isnull=False)
        context['departments_with_government_unit'] = FireDepartment.objects.filter(object_id__isnull=True)

        return context


class Home(CacheMixin, TemplateView):
    cache_timeout = 60 * 60 * 24
    template_name = 'firestation/home.html'

class DownloadShapefile(LoginRequiredMixin, View):
    content_type = 'application/zip'
    output_dir = '/tmp'

    def get_queryset(self, *args, **kwargs):
        return kwargs.get('department').firestation_set.all()

    def create_shapefile(self, queryset, filename, geom):
        path = mkdtemp()
        file_path = os.path.join(path, filename)
        geom_type = ogr.wkbPoint

        if queryset.model._meta.get_field_by_name(geom)[0].geom_type == 'MULTIPOLYGON':
            geom_type = ogr.wkbMultiPolygon

        driver = ogr.GetDriverByName("ESRI Shapefile")

        # create the data source
        data_source = driver.CreateDataSource(file_path)

        # create the spatial reference, WGS84
        srs = osr.SpatialReference()
        srs.ImportFromEPSG(4326)

        # create the layer
        layer = data_source.CreateLayer(filename, srs, geom_type)

        # Name, Type, Width
        fields = [
            ('id', ogr.OFTInteger, None),
            ('name', ogr.OFTString, 254),
            ('department', ogr.OFTInteger, None),
            ('station_nu', ogr.OFTInteger, None),
            ('address_l1', ogr.OFTString, 100),
            ('address_l2', ogr.OFTString, 100),
            ('city', ogr.OFTString, 50),
            ('state', ogr.OFTString, 40),
            ('zipcode', ogr.OFTString, 10),
            ('country', ogr.OFTString, 2),
        ]

        ids = queryset.values_list('id', flat=True)

        # Pivot staffing rows per apparatus without aggregating
        # resulting in multiple columns if a station has more than one of any apparatus type
        for apparatus, apparatus_alias in Staffing.APPARATUS_SHAPEFILE_CHOICES:

            cursor = connection.cursor()
            sql = "select count(*) from firestation_staffing  where firestation_id in %s and apparatus=%s " \
                  "group by firestation_id order by count(*) desc limit 1;"
            cursor.execute(sql, [tuple(ids), apparatus])
            row = cursor.fetchone()
            count = 0

            if row:
                count = row[0]

            for n in range(0, count) or [0]:
                field_name = apparatus_alias

                if n > 0:
                    field_name = apparatus_alias + '_{0}'.format(n)

                fields.append((field_name, ogr.OFTInteger, None))

        for alias, field_type, width in fields:
            field = ogr.FieldDefn(alias, field_type)

            if width:
                field.SetWidth(width)

            layer.CreateField(field)
            field = None

        # Process the text file and add the attributes and features to the shapefile
        for row in queryset:

            raw_geom = getattr(row, geom)
            wkt = None

            if raw_geom:
              wkt = raw_geom.wkt

            else:
                continue

            feature = ogr.Feature(layer.GetLayerDefn())

            # Set the attributes using the values from the delimited text file
            feature.SetField('id', row.id)
            feature.SetField('name', str(row.name))
            feature.SetField('department', row.department.id)
            feature.SetField('station_nu', row.station_number)

            feature.SetField('address_l1', str(getattr(row.station_address, 'address_line1', str)) or None)
            feature.SetField('address_l2', str(getattr(row.station_address, 'address_line2', str)) or None)
            feature.SetField('city', str(getattr(row.station_address, 'city', str)) or None)
            feature.SetField('state', str(getattr(row.station_address, 'state_province', str)) or None)
            feature.SetField('zipcode', str(getattr(row.station_address, 'postal_code', str)) or None)
            feature.SetField('country', str(getattr(row.station_address, 'country_id', str)) or None)

            # Populate staffing for each unit
            for apparatus, apparatus_alias in Staffing.APPARATUS_SHAPEFILE_CHOICES:

                for n, record in enumerate(row.staffing_set.filter(apparatus=apparatus)):
                    apparatus_alias_index = apparatus_alias

                    if n > 0:
                        apparatus_alias_index += '_{0}'.format(n)

                    feature.SetField(apparatus_alias_index, record.personnel)


            # Create the point from the Well Known Txt
            point = ogr.CreateGeometryFromWkt(wkt)

            # Set the feature geometry using the point
            feature.SetGeometry(point)
            # Create the feature in the layer (shapefile)
            layer.CreateFeature(feature)
            # Destroy the feature to free resources
            feature.Destroy()

        # Destroy the data source to free resources
        data_source.Destroy()

        if os.path.exists(os.path.join(self.output_dir, filename + '.zip')):
            filename += '-' + str(uuid.uuid4())[:5]

        zip_file = shutil.make_archive(self.output_dir + '/' + filename, 'zip', file_path)

        if self.request.META.get('SERVER_NAME') != 'testserver':
            remove_file.delay(path)
            remove_file.delay(zip_file, countdown=600)

        return file_path, zip_file

    def filename(self, *args, **kwargs):
        return '{0}-{1}-{2}'.format(kwargs.get('department').id,
                                    kwargs.get('department').slug,
                                    kwargs.get('geom_type'))

    def get(self, request, *args, **kwargs):
        response = HttpResponse(content_type=self.content_type)
        geom = self.kwargs.get('geometry_field', 'geom')

        geom_type = 'stations'

        if geom == 'district':
            geom_type = 'districts'

        department = get_object_or_404(FireDepartment, id=kwargs['pk'])
        queryset = self.get_queryset(department=department)
        filename = self.filename(department=department, geom_type=geom_type)
        file_path, zip_file = self.create_shapefile(queryset, filename, geom)
        response['Content-Disposition'] = 'attachment; filename="{0}.zip"'.format(smart_str(filename))
        response['X-Accel-Redirect'] = smart_str(zip_file)
        response.content = file_path
        return response
