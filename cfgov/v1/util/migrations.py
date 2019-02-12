import json
import six

from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction

from wagtail.wagtailcore.blocks import StreamValue

from treebeard.mp_tree import MP_Node

try:  # pragma: no cover
    from collections.abc import Sequence
except ImportError:  # pragma: no cover
    from collections import Sequence


def get_page(page_cls, slug):
    return page_cls.objects.get(slug=slug)


def get_free_path(apps, parent_page):
    offset = 1
    base_page_cls = apps.get_model('wagtailcore', 'Page')

    while True:
        path = MP_Node._get_path(
            parent_page.path,
            parent_page.depth + 1,
            parent_page.numchild + offset
        )

        try:
            base_page_cls.objects.get(path=path)
        except base_page_cls.DoesNotExist:
            return path

        offset += 1


@transaction.atomic
def get_or_create_page(apps, page_cls_app, page_cls_name, title, slug,
                       parent_page, live=False, **kwargs):
    page_cls = apps.get_model(page_cls_app, page_cls_name)

    try:
        return get_page(page_cls, slug)
    except ObjectDoesNotExist:
        pass

    ContentType = apps.get_model('contenttypes.ContentType')

    page_content_type = ContentType.objects.get_for_model(page_cls)

    parent_page = get_page(parent_page.specific_class, parent_page.slug)

    page = page_cls.objects.create(
        title=title,
        slug=slug,
        depth=parent_page.depth + 1,
        path=get_free_path(apps, parent_page),
        content_type=page_content_type,
        live=live,
        **kwargs
    )

    parent_page.numchild += 1
    parent_page.save()

    return page


def is_page(page_or_revision):
    """ Return True if the page_or_revision is a Page object """
    return not hasattr(page_or_revision, 'content_json')


def get_stream_data(page_or_revision, field_name):
    """ Get the stream field data for a given field name on a page or a
    revision """
    if is_page(page_or_revision):
        field = getattr(page_or_revision, field_name)
        stream_block = field.stream_block
        stream_data = stream_block.get_prep_value(field)
    else:
        revision_content = json.loads(page_or_revision.content_json)
        field = revision_content.get(field_name)
        stream_data = json.loads(field) if field else []

    return stream_data


def set_stream_data(page_or_revision, field_name, stream_data, commit=True):
    """ Set the stream field data for a given field name on a page or a
    revision. If commit is True (default) save() is called on the
    page_or_revision object. """
    if is_page(page_or_revision):
        field = getattr(page_or_revision, field_name)
        stream_block = field.stream_block
        stream_value = StreamValue(stream_block, stream_data, is_lazy=True)
        setattr(page_or_revision, field_name, stream_value)
    else:
        revision_content = json.loads(page_or_revision.content_json)
        revision_content[field_name] = json.dumps(stream_data)
        page_or_revision.content_json = json.dumps(revision_content)

    if commit:
        page_or_revision.save()


def migrate_stream_data(page_or_revision, block_type, stream_data, mapper):
    """ Recursively run the mapper on fields of block_type in stream_data """
    migrated = False
    new_stream_data = []

    for field in stream_data:
        if block_type == field['type']:
            field['value'] = mapper(page_or_revision, field['value'])
            migrated = True
        elif (isinstance(field['value'], Sequence) and
              not isinstance(field['value'], six.string_types)):
            field['value'], migrated = migrate_stream_data(
                page_or_revision, block_type, field['value'], mapper
            )

        new_stream_data.append(field)

    return new_stream_data, migrated


def migrate_stream_field(page_or_revision, field_name, block_type, mapper):
    """ Migrate blocks of the type within a StreamField of the name belonging
    to the page or revision using the mapper function """
    stream_data = get_stream_data(page_or_revision, field_name)

    new_stream_data, migrated = migrate_stream_data(
        page_or_revision, block_type, stream_data, mapper
    )

    if migrated:
        set_stream_data(page_or_revision, field_name, new_stream_data)


@transaction.atomic
def migrate_page_types_and_fields(apps, page_types_and_fields, mapper):
    """ Migrate Wagtail StreamFields using the given mapper function.
        page_types_and_fields should be a list of 4-tuples
        providing ('app', 'PageType', 'field_name', 'block type'). """
    for app, page_type, field_name, block_type in page_types_and_fields:
        page_model = apps.get_model(app, page_type)
        revision_model = apps.get_model('wagtailcore.PageRevision')
        for page in page_model.objects.all():
            migrate_stream_field(page, field_name, block_type, mapper)

            revisions = revision_model.objects.filter(
                page=page).order_by('-id')
            for revision in revisions:
                migrate_stream_field(revision, field_name, block_type, mapper)
