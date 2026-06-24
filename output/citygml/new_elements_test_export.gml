<?xml version='1.0' encoding='UTF-8'?>
<CityModel xmlns="http://www.opengis.net/citygml/2.0" xmlns:xAL="urn:oasis:names:tc:ciq:xsdschema:xAL:2.0" xmlns:gml="http://www.opengis.net/gml" xmlns:wtr="http://www.opengis.net/citygml/waterbody/2.0" xmlns:app="http://www.opengis.net/citygml/appearance/2.0" xmlns:veg="http://www.opengis.net/citygml/vegetation/2.0" xmlns:dem="http://www.opengis.net/citygml/relief/2.0" xmlns:tran="http://www.opengis.net/citygml/transportation/2.0" xmlns:bldg="http://www.opengis.net/citygml/building/2.0" xmlns:grp="http://www.opengis.net/citygml/cityobjectgroup/2.0" xmlns:tun="http://www.opengis.net/citygml/tunnel/2.0" xmlns:frn="http://www.opengis.net/citygml/cityfurniture/2.0" xmlns:gen="http://www.opengis.net/citygml/generics/2.0" xmlns:brid="http://www.opengis.net/citygml/bridge/2.0" xmlns:xlink="http://www.w3.org/1999/xlink" xmlns:luse="http://www.opengis.net/citygml/landuse/2.0" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:schemaLocation="http://www.opengis.net/citygml/building/2.0 http://schemas.opengis.net/citygml/building/2.0/building.xsd http://www.opengis.net/citygml/cityobjectgroup/2.0 http://schemas.opengis.net/citygml/cityobjectgroup/2.0/cityObjectGroup.xsd http://www.opengis.net/citygml/tunnel/2.0 http://schemas.opengis.net/citygml/tunnel/2.0/tunnel.xsd http://www.opengis.net/citygml/waterbody/2.0 http://schemas.opengis.net/citygml/waterbody/2.0/waterBody.xsd http://www.opengis.net/citygml/appearance/2.0 http://schemas.opengis.net/citygml/appearance/2.0/appearance.xsd http://www.opengis.net/citygml/cityfurniture/2.0 http://schemas.opengis.net/citygml/cityfurniture/2.0/cityFurniture.xsd http://www.opengis.net/citygml/bridge/2.0 http://schemas.opengis.net/citygml/bridge/2.0/bridge.xsd http://www.opengis.net/citygml/generics/2.0 http://schemas.opengis.net/citygml/generics/2.0/generics.xsd http://www.opengis.net/citygml/vegetation/2.0 http://schemas.opengis.net/citygml/vegetation/2.0/vegetation.xsd http://www.opengis.net/citygml/relief/2.0 http://schemas.opengis.net/citygml/relief/2.0/relief.xsd http://www.opengis.net/citygml/transportation/2.0 http://schemas.opengis.net/citygml/transportation/2.0/transportation.xsd http://www.opengis.net/citygml/landuse/2.0 http://schemas.opengis.net/citygml/landuse/2.0/landUse.xsd">
  <gml:name>NewElementsTest</gml:name>
  <gml:boundedBy>
    <gml:Envelope srsName="urn:ogc:def:crs:EPSG::25832" srsDimension="3">
      <gml:lowerCorner srsDimension="3">0 0 0</gml:lowerCorner>
      <gml:upperCorner srsDimension="3">10 10 6</gml:upperCorner>
    </gml:Envelope>
  </gml:boundedBy>
  <cityObjectMember>
    <bldg:Building gml:id="BLDG_NEW_1">
      <gml:description>Building exercising newly mapped elements</gml:description>
      <gml:name>Test Building 1</gml:name>
      <gml:boundedBy>
        <gml:Envelope srsName="urn:ogc:def:crs:EPSG::25832" srsDimension="3">
          <gml:lowerCorner srsDimension="3">0 0 0</gml:lowerCorner>
          <gml:upperCorner srsDimension="3">10 10 6</gml:upperCorner>
        </gml:Envelope>
      </gml:boundedBy>
      <creationDate>2024-01-01</creationDate>
      <relativeToTerrain>entirelyAboveTerrain</relativeToTerrain>
      <terminationDate>2030-12-31</terminationDate>
      <gml:metaDataProperty xmlns:test="http://example.com/ade/1.0">
        <gml:GenericMetaData>
          <test:author>jdoe</test:author>
        </gml:GenericMetaData>
      </gml:metaDataProperty>
      <generalizesTo xmlns:test="http://example.com/ade/1.0" xlink:href="#BLDG_NEW_1_part0"/>
      <externalReference xmlns:test="http://example.com/ade/1.0">
        <informationSystem>http://gdi.example/alkis</informationSystem>
        <externalObject>
          <name>EXT_OBJ_42</name>
        </externalObject>
      </externalReference>
      <test:customAttribute xmlns:test="http://example.com/ade/1.0">ade-payload-value</test:customAttribute>
      <gml:location xmlns:test="http://example.com/ade/1.0">
        <gml:Point srsName="urn:ogc:def:crs:EPSG::25832" srsDimension="3">
          <gml:pos>5 5 0</gml:pos>
        </gml:Point>
      </gml:location>
      <gen:stringAttribute name="Gemeindeschluessel">
        <gen:value>02000000</gen:value>
      </gen:stringAttribute>
      <bldg:class>1000</bldg:class>
      <bldg:function>1000</bldg:function>
      <bldg:usage>1000</bldg:usage>
      <bldg:yearOfConstruction>1985</bldg:yearOfConstruction>
      <bldg:roofType>1000</bldg:roofType>
      <bldg:measuredHeight uom="urn:adv:uom:m">6.0</bldg:measuredHeight>
      <bldg:storeysAboveGround>2</bldg:storeysAboveGround>
      <bldg:storeysBelowGround>1</bldg:storeysBelowGround>
      <bldg:storeyHeightsAboveGround uom="urn:adv:uom:m">3.0 3.0</bldg:storeyHeightsAboveGround>
      <bldg:storeyHeightsBelowGround uom="urn:adv:uom:m">2.5</bldg:storeyHeightsBelowGround>
      <bldg:boundedBy>
        <bldg:WallSurface gml:id="WALL_1">
          <bldg:lod2MultiSurface>
            <gml:MultiSurface gml:id="ID_1c215b2e-fab7-4fb3-9b9b-9888dfa70499">
              <gml:surfaceMember>
                <gml:Polygon gml:id="POLY_WALL_1">
                  <gml:exterior>
                    <gml:LinearRing gml:id="ring_ext_POLY_WALL_1">
                      <gml:posList srsDimension="3">0 0 0 10 0 0 10 0 6 0 0 6 0 0 0</gml:posList>
                    </gml:LinearRing>
                  </gml:exterior>
                </gml:Polygon>
              </gml:surfaceMember>
            </gml:MultiSurface>
          </bldg:lod2MultiSurface>
        </bldg:WallSurface>
      </bldg:boundedBy>
      <bldg:lod2MultiCurve>
        <gml:MultiCurve>
          <gml:curveMember>
            <gml:LineString>
              <gml:posList srsDimension="3">0 0 6 10 0 6 10 10 6</gml:posList>
            </gml:LineString>
          </gml:curveMember>
        </gml:MultiCurve>
      </bldg:lod2MultiCurve>
      <bldg:interiorBuildingInstallation>
        <bldg:IntBuildingInstallation gml:id="INTINST_1">
          <gml:name>Staircase</gml:name>
          <bldg:lod4Geometry>
            <gml:MultiSurface gml:id="ms_INTINST_1_lod4geom">
              <gml:surfaceMember>
                <gml:Polygon gml:id="POLY_INST_1">
                  <gml:exterior>
                    <gml:LinearRing gml:id="ring_ext_POLY_INST_1">
                      <gml:posList srsDimension="3">1 1 0 2 1 0 2 1 3 1 1 3 1 1 0</gml:posList>
                    </gml:LinearRing>
                  </gml:exterior>
                </gml:Polygon>
              </gml:surfaceMember>
            </gml:MultiSurface>
          </bldg:lod4Geometry>
        </bldg:IntBuildingInstallation>
      </bldg:interiorBuildingInstallation>
      <bldg:consistsOfBuildingPart>
        <bldg:BuildingPart gml:id="BLDG_NEW_1_part0">
          <gml:name>Annex</gml:name>
          <bldg:function>1000</bldg:function>
          <bldg:measuredHeight uom="urn:adv:uom:m">3.0</bldg:measuredHeight>
          <bldg:storeysAboveGround>1</bldg:storeysAboveGround>
          <bldg:boundedBy>
            <bldg:RoofSurface gml:id="PART_ROOF_1">
              <bldg:lod2MultiSurface>
                <gml:MultiSurface gml:id="ID_35f4c231-225c-4d0e-97cd-047b02ca4744">
                  <gml:surfaceMember>
                    <gml:Polygon gml:id="POLY_PART_ROOF_1">
                      <gml:exterior>
                        <gml:LinearRing gml:id="ring_ext_POLY_PART_ROOF_1">
                          <gml:posList srsDimension="3">0 0 6 10 0 6 10 10 6 0 10 6 0 0 6</gml:posList>
                        </gml:LinearRing>
                      </gml:exterior>
                    </gml:Polygon>
                  </gml:surfaceMember>
                </gml:MultiSurface>
              </bldg:lod2MultiSurface>
            </bldg:RoofSurface>
          </bldg:boundedBy>
        </bldg:BuildingPart>
      </bldg:consistsOfBuildingPart>
    </bldg:Building>
  </cityObjectMember>
</CityModel>
