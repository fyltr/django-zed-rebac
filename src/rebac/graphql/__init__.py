"""GraphQL adapters for ``django-zed-rebac``.

Currently ships the Strawberry/Channels adapter under
:mod:`rebac.graphql.strawberry`. Behind the ``[strawberry]`` extra —
importing the module without ``strawberry-graphql`` installed raises a
plain ``ImportError`` naming the missing package.

Future Graphene / Ariadne adapters would land alongside as
``rebac.graphql.graphene`` / ``rebac.graphql.ariadne``; same
extras-driven pattern.
"""
