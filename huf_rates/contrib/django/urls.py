from rest_framework.routers import DefaultRouter

from .views import CurrencyConverterView

router = DefaultRouter()
router.register(r'', CurrencyConverterView, basename='currency_converter')
urlpatterns = router.urls
